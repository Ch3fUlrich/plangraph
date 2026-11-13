use crate::go_gate::GoRow;
use crate::handoff::re;
use serde_json::Value;
#[derive(Debug, Clone, serde::Serialize)]
pub struct Approval {
    #[serde(flatten)]
    pub row: GoRow,
    pub form: String,
    pub pr: Option<String>,
}
fn after_token(s: &str, at: usize) -> bool {
    s[at..]
        .chars()
        .next()
        .is_none_or(|c| c.is_whitespace() || ",;:.".contains(c))
}
fn valid_hash_end(s: &str, at: usize) -> bool {
    s[at..]
        .chars()
        .next()
        .is_none_or(|c| c != '-' && !re(r"^\w$").is_match(&c.to_string()))
}
pub fn parse_row(row: &str) -> Option<Approval> {
    let mut parsed = crate::go_gate::parse_row(row, true)?;
    let decision = &parsed.decision;
    let refusal = re(r"(?i)\b(?:NO[\s-]+GO|FAIL)\b");
    let approval = re(
        r#"^(?:(?:MERGE\s+|Conditional\s+)?GO(?:-HEAD)?(?:\s+#(?P<go_pr>\d+)\b)?|(?:PR\s*)?#(?P<pr>\d+)\s+(?:[A-Z0-9][A-Za-z0-9_./+-]*\s+(?:[A-Za-z0-9][A-Za-z0-9_./+-]*\s+|\([^;:"'`]*\)\s+)*)?GO)"#,
    );
    let mut selected = None;
    for start in
        std::iter::once(0).chain(decision.match_indices(['.', ';', ':']).map(|(i, _)| i + 1))
    {
        let prefix = if start == 0 {
            re(r"^\s*(?:\([^)]*\)\s*)*")
        } else {
            re(r"^\s*")
        };
        let offset = start + prefix.find(&decision[start..]).unwrap().end();
        if let Some(c) = approval.captures(&decision[offset..]) {
            let all = c.get(0).unwrap();
            if !after_token(decision, offset + all.end()) || refusal.is_match(all.as_str()) {
                continue;
            }
            selected = Some(
                c.name("go_pr")
                    .or_else(|| c.name("pr"))
                    .map(|m| m.as_str().to_owned()),
            );
            break;
        }
    }
    let pr = selected?;
    for refused in refusal.find_iter(decision) {
        let following = re(r"^\s+(?:PR\s*)?#(\d+)\b")
            .captures(&decision[refused.end()..])
            .map(|c| c[1].to_owned());
        let preceding = re(r"(?:PR\s*)?#(\d+)\b")
            .captures_iter(&decision[..refused.start()])
            .last()
            .map(|c| c[1].to_owned());
        let refused_pr = following.or(preceding);
        if pr.is_none() || refused_pr.is_none() || pr == refused_pr {
            return None;
        }
    }
    let mut heads = vec![];
    for c in re(r"\bGO-HEAD\s+([0-9a-fA-F]{8,40})\b").captures_iter(decision) {
        let m = c.get(1).unwrap();
        if !valid_hash_end(decision, m.end()) {
            continue;
        }
        let sha = m.as_str().to_lowercase();
        if !heads.contains(&sha) {
            heads.push(sha);
        }
    }
    if !heads.is_empty() {
        if heads.len() != 1 {
            return None;
        }
        parsed.heads = heads;
        return Some(Approval {
            row: parsed,
            form: "go-head".into(),
            pr: None,
        });
    }
    let c = re(r"^\s*GO\s+#(\d+)\s+([0-9a-fA-F]{8,40})\b").captures(decision)?;
    if !valid_hash_end(decision, c.get(2)?.end()) {
        return None;
    }
    let pr = c[1].to_owned();
    parsed.heads = vec![c[2].into()];
    Some(Approval {
        row: parsed,
        form: "go-pr-prefix".into(),
        pr: Some(pr),
    })
}

#[derive(Debug, Clone)]
pub struct Merge {
    pub key: String,
    pub sha: String,
    pub head: String,
    pub pr: Option<String>,
}
pub fn merges_from_json(v: &Value) -> Vec<Merge> {
    v.as_object()
        .into_iter()
        .flatten()
        .map(|(key, val)| Merge {
            key: key.clone(),
            sha: val[0].as_str().unwrap_or("").into(),
            head: val[1].as_str().unwrap_or("").into(),
            pr: val[2].as_str().map(str::to_owned),
        })
        .collect()
}
pub fn merged_items(text: &str) -> Vec<Merge> {
    let fields: Vec<_> = text.split('\0').collect();
    let mut out: Vec<Merge> = vec![];
    for f in fields.as_chunks::<4>().0 {
        let parents: Vec<_> = f[1].split_whitespace().collect();
        if parents.len() < 2 {
            continue;
        }
        let pr = re(r"^Merge (?:PR|pull request) #(\d+)\b")
            .captures(f[3].trim())
            .map(|c| c[1].to_owned());
        for key in f[2].lines().map(str::trim) {
            if !out.iter().any(|m| m.key == key) {
                out.push(Merge {
                    key: key.into(),
                    sha: f[0].trim().into(),
                    head: parents[1].into(),
                    pr: pr.clone(),
                });
            }
        }
    }
    out
}
pub const HEADER: [&str; 7] = [
    "key",
    "merge_sha",
    "pr_head_sha",
    "go_did",
    "go_row_form",
    "seat_run_ids",
    "action",
];
pub const ACTIONS: [&str; 4] = [
    "post-verdicts",
    "close-by-decision",
    "no-go-row",
    "not-merged",
];
pub struct Report {
    pub rows: Vec<Vec<String>>,
    pub missing: Vec<Vec<String>>,
    pub counts: [usize; 4],
}
impl Report {
    pub fn summary(&self) -> String {
        ACTIONS
            .iter()
            .zip(self.counts)
            .map(|(k, n)| format!("{k}={n}"))
            .collect::<Vec<_>>()
            .join(" ")
    }
}
pub fn report(items: &[Value], merged: &[Merge], log: &str) -> Report {
    let mut decisions: Vec<_> = log.lines().filter_map(parse_row).collect();
    decisions.sort_by_key(|a| a.form != "go-head");
    let decisions: Vec<_> = decisions
        .into_iter()
        .map(|a| {
            let keys: Vec<_> = merged
                .iter()
                .filter(|m| {
                    m.head
                        .to_lowercase()
                        .starts_with(&a.row.heads[0].to_lowercase())
                        && (a.pr.is_none() || m.pr.is_none() || a.pr == m.pr)
                })
                .map(|m| m.key.clone())
                .collect();
            (a, keys)
        })
        .collect();
    let mut report = Report {
        rows: vec![],
        missing: vec![],
        counts: [0; 4],
    };
    for item in items {
        let Some(key) = item["key"].as_str() else {
            continue;
        };
        let m = merged.iter().find(|m| m.key == key);
        let go = decisions
            .iter()
            .find(|(_, keys)| keys.as_slice() == [key])
            .map(|(a, _)| a);
        let action = if m.is_none() {
            3
        } else if go.is_none() {
            2
        } else if !go.unwrap().row.runs.is_empty() {
            0
        } else {
            1
        };
        let row = vec![
            key.into(),
            m.map_or("", |m| &m.sha).into(),
            m.map_or("", |m| &m.head).into(),
            go.map_or("", |g| &g.row.did).into(),
            go.map_or("", |g| &g.form).into(),
            go.map_or(String::new(), |g| g.row.runs.join(",")),
            ACTIONS[action].into(),
        ];
        if m.is_some() {
            report.rows.push(row.clone());
        }
        if action >= 2 {
            report.missing.push(row);
        }
        report.counts[action] += 1;
    }
    report
}
pub fn tsv(rows: &[Vec<String>]) -> String {
    let quote = |s: &str| {
        if s.contains(['\t', '\n', '\r', '"']) {
            format!("\"{}\"", s.replace('"', "\"\""))
        } else {
            s.to_owned()
        }
    };
    let mut text = HEADER.join("\t") + "\n";
    for row in rows {
        text += &row.iter().map(|s| quote(s)).collect::<Vec<_>>().join("\t");
        text.push('\n');
    }
    text
}
