//! The command layer of `land-train relane` and `relane-open`: reads the lane records, runs `relane` over them
//! parents first, and renders one result line per lane.
use super::relane::{relane, Lane, Outcome};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;

/// What the registry recorded for one lane: its head and the identity of its writer, and the open parent of
/// a stacked lane (the lane's own commits start after the parent's recorded head).
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Spec {
    pub branch: String,
    pub head: String,
    pub writer: String,
    #[serde(default)]
    pub parent: Option<ParentSpec>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct ParentSpec {
    pub branch: String,
    pub head: String,
}

/// Relanes the lanes in the order given (a parent before its child), one after the other: one clone never
/// fetches into or pushes two lanes at the same time. A child is rebased onto its parent's new head.
pub fn relane_all(
    repo: &std::path::Path,
    remote: &str,
    onto: &str,
    push: bool,
    specs: &[Spec],
) -> Result<Vec<String>, String> {
    relane_all_with(repo, remote, onto, push, specs, None)
}

/// Appends one JSON line per relaned lane to `record`: the hand-off the seat evidence rule and the probe
/// move read (old head, new head, patch-id equality, counts, successor branch, the main it was relaned onto).
fn append_record(
    record: &std::path::Path,
    kind: &str,
    onto: &str,
    r: &super::relane::Report,
) -> Result<(), String> {
    use std::io::Write;
    let mut line = serde_json::to_value(r).map_err(|e| e.to_string())?;
    line["kind"] = kind.into();
    line["onto"] = onto.into();
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(record)
        .map_err(|e| format!("retry: relane record {}: {e}", record.display()))?;
    writeln!(file, "{line}").map_err(|e| format!("retry: relane record: {e}"))
}

pub fn relane_all_with(
    repo: &std::path::Path,
    remote: &str,
    onto: &str,
    push: bool,
    specs: &[Spec],
    record: Option<&std::path::Path>,
) -> Result<Vec<String>, String> {
    let mut lines = Vec::new();
    let (mut failed, mut retry) = (false, false);
    let mut heads: BTreeMap<&str, String> = BTreeMap::new();
    for s in specs {
        let lane_onto = s
            .parent
            .as_ref()
            .and_then(|p| heads.get(p.branch.as_str()).cloned())
            .unwrap_or_else(|| onto.to_owned());
        let outcome = match &s.parent {
            None => run_one(repo, remote, onto, push, s, None),
            Some(p) => match heads.get(p.branch.as_str()) {
                Some(new) => run_one(repo, remote, new, push, s, Some(&p.head)),
                None => Outcome::Refused {
                    lane: s.branch.clone(),
                    reason: format!("its parent {} was not relaned", p.branch),
                },
            },
        };
        match &outcome {
            Outcome::Current(r) | Outcome::Rebased(r) => {
                heads.insert(&s.branch, r.new_head.clone());
                if let Some(path) = record {
                    let kind = if matches!(outcome, Outcome::Current(_)) {
                        "CURRENT"
                    } else {
                        "REBASED"
                    };
                    append_record(path, kind, &lane_onto, r)?;
                }
            }
            Outcome::Retry { .. } => retry = true,
            _ => failed = true,
        }
        lines.push(outcome.line());
    }
    if retry {
        // Exit code 4: an environment failure the settlement must run again; the lines say which lanes.
        Err(format!("retry: {}", lines.join("\n")))
    } else if failed {
        Err(lines.join("\n"))
    } else {
        Ok(lines)
    }
}

fn run_one(
    repo: &std::path::Path,
    remote: &str,
    onto: &str,
    push: bool,
    s: &Spec,
    parent_head: Option<&str>,
) -> Outcome {
    // An environment failure of one lane is that lane's refusal; the others are still relaned.
    relane(&Lane {
        repo,
        remote,
        branch: &s.branch,
        onto,
        expected_head: &s.head,
        writer: &s.writer,
        parent_head,
        push,
    })
    .unwrap_or_else(|reason| Outcome::Retry {
        lane: s.branch.clone(),
        reason,
    })
}

fn need(flags: &BTreeMap<String, String>, k: &str) -> Result<String, String> {
    flags
        .get(k)
        .cloned()
        .ok_or_else(|| format!("relane: missing --{k}"))
}

/// `relane --repo DIR --remote NAME --onto SHA --spec FILE [--push true]`; FILE is a JSON array of
/// `{branch, head, writer, parent: {branch, head} | null}`, parents first.
pub fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let text =
        std::fs::read_to_string(need(flags, "spec")?).map_err(|e| format!("relane spec: {e}"))?;
    let specs: Vec<Spec> = serde_json::from_str(&text).map_err(|e| format!("relane spec: {e}"))?;
    relane_all_with(
        &PathBuf::from(need(flags, "repo")?),
        &need(flags, "remote")?,
        &need(flags, "onto")?,
        flags.get("push").is_some_and(|v| v == "true"),
        &specs,
        flags.get("record").map(std::path::Path::new),
    )
}

#[derive(Deserialize)]
struct Pr {
    #[serde(rename = "headRefName")]
    head_ref_name: String,
    #[serde(rename = "headRefOid")]
    head_ref_oid: String,
    #[serde(rename = "baseRefName")]
    base_ref_name: String,
    #[serde(default)]
    commits: Vec<PrCommit>,
}

#[derive(Deserialize)]
struct PrCommit {
    #[serde(rename = "messageBody", default)]
    message_body: String,
}

/// `lane/x-r4` -> `x`: the name every writer of the lane carries in its `Writer` trailer.
fn lane_stem(branch: &str) -> &str {
    let name = branch.strip_prefix("lane/").unwrap_or(branch);
    match name.rsplit_once("-r") {
        Some((stem, n)) if !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()) => stem,
        _ => name,
    }
}

/// The lane name in a `Writer` value `claude-x id (EFFORT writer NAME, harness, host, D-1)`: the field the
/// parenthetical note opens with. A later `writer` phrase in the note, or a host that equals another lane, is
/// not the lane.
fn writer_lane(writer: &str) -> Option<&str> {
    let (_, note) = writer.split_once(" (")?;
    let (_effort, rest) = note.split_once(' ')?;
    let name = rest
        .strip_prefix("writer ")?
        .split([',', ')'])
        .next()?
        .trim();
    (!name.is_empty()).then_some(name)
}

/// The `Writer` trailer value of the lane's newest commit that has one (a merge of main has none), when it
/// names the lane. The lane's name comes from the branch, not from its history, so a lane that holds only
/// another lane's commits cannot vouch for them; an empty value is refused by `relane`.
fn writer_of(pr: &Pr) -> String {
    pr.commits
        .iter()
        .rev()
        .find_map(|c| {
            c.message_body
                .lines()
                .rev()
                .find_map(|l| l.strip_prefix("Writer: "))
        })
        .map(str::trim)
        .filter(|w| writer_lane(w) == Some(lane_stem(&pr.head_ref_name)))
        .unwrap_or("")
        .to_owned()
}

/// The open `lane/` pull requests (from `gh pr list --json headRefName,headRefOid,baseRefName,commits`) as
/// specs, parents first. A lane whose writer does not name it gets an empty writer: `relane` refuses it by name.
pub fn specs_from_prs(json: &str) -> Result<Vec<Spec>, String> {
    let prs: Vec<Pr> = serde_json::from_str(json).map_err(|e| format!("retry: gh pr list: {e}"))?;
    let open: BTreeMap<&str, &Pr> = prs
        .iter()
        .filter(|p| p.head_ref_name.starts_with("lane/"))
        .map(|p| (p.head_ref_name.as_str(), p))
        .collect();
    let depth = |p: &Pr| {
        let (mut n, mut at) = (0, p);
        while let Some(parent) = open.get(at.base_ref_name.as_str()) {
            n += 1;
            at = parent;
            if n > open.len() {
                break;
            }
        }
        n
    };
    let mut lanes: Vec<&Pr> = open.values().copied().collect();
    lanes.sort_by_key(|p| (depth(p), p.head_ref_name.clone()));
    Ok(lanes
        .into_iter()
        .map(|p| Spec {
            branch: p.head_ref_name.clone(),
            head: p.head_ref_oid.clone(),
            writer: writer_of(p),
            parent: open.get(p.base_ref_name.as_str()).map(|q| ParentSpec {
                branch: q.head_ref_name.clone(),
                head: q.head_ref_oid.clone(),
            }),
        })
        .collect())
}

/// `relane-open --repo DIR --github OWNER/NAME --remote NAME --merge SHA [--push true] [--record FILE]`: relanes every open
/// `lane/` pull request onto the current tip of main, which must hold the train merge; the settlement step
/// `relane` calls it. Exit 4 (`retry:`) is a failure to run again, 3 a lane that cannot be relaned.
pub fn run_open_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let (repo, remote, merge) = (
        PathBuf::from(need(flags, "repo")?),
        need(flags, "remote")?,
        need(flags, "merge")?,
    );
    let retry = |e: String| format!("retry: {e}");
    super::relane_git::plain_run(
        &repo,
        &[
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            &remote,
            "+refs/heads/main:refs/po/main",
        ],
    )
    .map_err(retry)?;
    let tip = super::git::out(&repo, &["rev-parse", "--verify", "refs/po/main^{commit}"])
        .map_err(retry)?;
    // A merge the fetch of main did not bring is not on main.
    if !super::git::is_ancestor(&repo, &merge, &tip).unwrap_or(false) {
        return Err(retry(format!("train merge {merge} is not on main {tip}")));
    }
    let out = Command::new("gh")
        .args(["pr", "list", "--repo", &need(flags, "github")?])
        .args(["--state", "open", "--limit", "200"])
        .args(["--json", "headRefName,headRefOid,baseRefName,commits"])
        .output()
        .map_err(|e| retry(format!("gh: {e}")))?;
    if !out.status.success() {
        return Err(retry(format!(
            "gh pr list failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )));
    }
    let specs = specs_from_prs(&String::from_utf8_lossy(&out.stdout))?;
    relane_all_with(
        &repo,
        &remote,
        &tip,
        flags.get("push").is_some_and(|v| v == "true"),
        &specs,
        flags.get("record").map(std::path::Path::new),
    )
}
