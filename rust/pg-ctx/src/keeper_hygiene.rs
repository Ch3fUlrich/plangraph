//! Report-only hygiene and fail-closed per-item cancellation dispositions.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Serialize)]
pub struct Issue {
    pub key: String,
    pub class: String,
    pub related: Vec<String>,
}

pub fn decision_ids(text: &str) -> BTreeSet<String> {
    let bytes = text.as_bytes();
    let mut ids = BTreeSet::new();
    for i in 0..bytes.len().saturating_sub(2) {
        if &bytes[i..i + 2] != b"D-"
            || i > 0 && (bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'-')
        {
            continue;
        }
        let digits: String = text[i + 2..]
            .chars()
            .take_while(char::is_ascii_digit)
            .collect();
        if digits.is_empty() || digits.len() > 6 {
            continue;
        }
        let end = i + 2 + digits.len();
        if bytes
            .get(end)
            .is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'-')
        {
            continue;
        }
        if let Ok(n) = digits.parse::<u64>() {
            ids.insert(format!("D-{n}"));
        }
    }
    ids
}

pub fn lint(items: &[Value], decisions: &BTreeSet<String>, now: i64) -> Result<Vec<Issue>, String> {
    let mut titles: BTreeMap<(String, String), Vec<String>> = BTreeMap::new();
    for item in items {
        let key = item["key"].as_str().ok_or("item key missing")?;
        let title: String = item["title"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .chars()
            .map(|c| if c.is_alphanumeric() { c } else { ' ' })
            .collect();
        let title = title.split_whitespace().collect::<Vec<_>>().join(" ");
        if !title.is_empty() {
            titles
                .entry((key.split(':').next().unwrap_or("").into(), title))
                .or_default()
                .push(key.into());
        }
    }
    let mut issues = Vec::new();
    for item in items {
        if matches!(item["state"].as_str(), Some("done" | "cancelled")) {
            continue;
        }
        let key = item["key"].as_str().ok_or("item key missing")?;
        let last = [
            item["updated_at"].as_str(),
            item["last_activity"]["at"].as_str(),
        ]
        .into_iter()
        .flatten()
        .filter_map(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.timestamp())
        .max();
        if last.is_some_and(|t| now.saturating_sub(t) > 30 * 86400) {
            issues.push(Issue {
                key: key.into(),
                class: "stale>30d".into(),
                related: vec![],
            });
        }
        for twins in titles
            .values()
            .filter(|v| v.len() > 1 && v.iter().any(|k| k == key))
        {
            issues.push(Issue {
                key: key.into(),
                class: "title-twin".into(),
                related: twins.iter().filter(|k| *k != key).cloned().collect(),
            });
        }
        let text = ["title", "description", "done_criteria", "note"]
            .iter()
            .filter_map(|field| item[field].as_str())
            .collect::<Vec<_>>()
            .join(" ");
        let dead = decision_ids(&text)
            .difference(decisions)
            .cloned()
            .collect::<Vec<_>>();
        if !dead.is_empty() {
            issues.push(Issue {
                key: key.into(),
                class: "dead-D-citation".into(),
                related: dead,
            });
        }
    }
    issues.sort_by(|a, b| (&a.key, &a.class).cmp(&(&b.key, &b.class)));
    Ok(issues)
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Disposition {
    pub key: String,
    pub item_updated_at: String,
    pub checked_at: u64,
    pub checked_repos: BTreeSet<String>,
    pub run_window_start: u64,
    pub claims: Vec<String>,
    pub runs: Vec<String>,
    pub open_prs: Vec<String>,
    pub branches: Vec<String>,
    pub lanes: Vec<String>,
    pub merged_commits: Vec<String>,
    pub dependents: Vec<String>,
    pub gate_edges: Vec<String>,
    pub operator_flags: Vec<String>,
    pub premise_checked: bool,
    pub reason: Reason,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Reason {
    PremiseSuperseded {
        rule: String,
    },
    Twin {
        key: String,
        dc_covers: bool,
        unique_dc_moved: bool,
    },
}

pub fn validate(
    item: &Value,
    d: &Disposition,
    repos: &BTreeSet<String>,
    now: u64,
) -> Result<(), String> {
    if item["key"].as_str() != Some(&d.key)
        || item["updated_at"].as_str() != Some(&d.item_updated_at)
        || item["state"].as_str() != Some("open")
        || item["operator"].as_bool() != Some(false)
    {
        return Err("item changed, claimed, terminal or operator protected".into());
    }
    if repos.is_empty()
        || repos != &d.checked_repos
        || d.checked_at > now
        || now - d.checked_at > 3600
        || d.run_window_start > now.saturating_sub(14 * 86400)
        || !d.premise_checked
    {
        return Err("incomplete, stale or unattributed checks".into());
    }
    if [
        &d.claims,
        &d.runs,
        &d.open_prs,
        &d.branches,
        &d.lanes,
        &d.merged_commits,
        &d.dependents,
        &d.gate_edges,
        &d.operator_flags,
    ]
    .iter()
    .any(|checks| !checks.is_empty())
    {
        return Err("cancellation has a protected reference or recent run".into());
    }
    match &d.reason {
        Reason::PremiseSuperseded { rule } => {
            let parts: Vec<_> = rule.split('.').collect();
            if parts.len() < 2
                || parts
                    .iter()
                    .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
            {
                return Err("superseded premise requires a rule n.m".into());
            }
        }
        Reason::Twin {
            key,
            dc_covers,
            unique_dc_moved,
        } => {
            if key.is_empty() || key == &d.key || !dc_covers || !unique_dc_moved {
                return Err("twin must cover every DC and receive unique parts first".into());
            }
        }
    }
    Ok(())
}

/// Decisions authority rows only; callers must supply an origin-backed export.
pub fn decision_rows(text: &str) -> BTreeMap<String, Vec<String>> {
    let mut rows: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut fence = false;
    for line in text.lines() {
        if line.trim_start().starts_with("```") || line.trim_start().starts_with("~~~") {
            fence = !fence;
            continue;
        }
        if fence || line.starts_with("    ") || line.starts_with('\t') {
            continue;
        }
        let cells: Vec<_> = line.split('|').collect();
        if !line.starts_with("| D-") || cells.len() < 5 {
            continue;
        }
        let ids = decision_ids(cells[1]);
        if ids.len() == 1 {
            rows.entry(ids.into_iter().next().unwrap())
                .or_default()
                .push(cells[3].trim().into());
        }
    }
    rows
}
