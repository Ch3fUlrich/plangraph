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
    let mut lines = Vec::new();
    let mut failed = false;
    let mut heads: BTreeMap<&str, String> = BTreeMap::new();
    for s in specs {
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
            }
            _ => failed = true,
        }
        lines.push(outcome.line());
    }
    if failed {
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
    .unwrap_or_else(|reason| Outcome::Refused {
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
    relane_all(
        &PathBuf::from(need(flags, "repo")?),
        &need(flags, "remote")?,
        &need(flags, "onto")?,
        flags.get("push").is_some_and(|v| v == "true"),
        &specs,
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

/// The `Writer` trailer value of the lane's newest commit: the writer the lane was recorded with.
fn writer_of(pr: &Pr) -> Option<String> {
    let body = &pr.commits.last()?.message_body;
    body.lines()
        .rev()
        .find_map(|l| l.strip_prefix("Writer: "))
        .map(|w| w.trim().to_owned())
        .filter(|w| !w.is_empty())
}

/// The open `lane/` pull requests (from `gh pr list --json headRefName,headRefOid,baseRefName,commits`) as
/// specs, parents first. A lane without a `Writer` trailer on its newest commit is left out: it has no
/// recorded identity to check.
pub fn specs_from_prs(json: &str) -> Result<Vec<Spec>, String> {
    let prs: Vec<Pr> = serde_json::from_str(json).map_err(|e| format!("gh pr list: {e}"))?;
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
        .filter_map(|p| {
            Some(Spec {
                branch: p.head_ref_name.clone(),
                head: p.head_ref_oid.clone(),
                writer: writer_of(p)?,
                parent: open.get(p.base_ref_name.as_str()).map(|q| ParentSpec {
                    branch: q.head_ref_name.clone(),
                    head: q.head_ref_oid.clone(),
                }),
            })
        })
        .collect())
}

/// `relane-open --repo DIR --github OWNER/NAME --remote NAME --onto SHA [--push true]`: relanes every open
/// `lane/` pull request after a train merge; the post-merge settlement step calls it.
pub fn run_open_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let out = Command::new("gh")
        .args(["pr", "list", "--repo", &need(flags, "github")?])
        .args(["--state", "open", "--limit", "200"])
        .args(["--json", "headRefName,headRefOid,baseRefName,commits"])
        .output()
        .map_err(|e| format!("gh: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "gh pr list failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let specs = specs_from_prs(&String::from_utf8_lossy(&out.stdout))?;
    relane_all(
        &PathBuf::from(need(flags, "repo")?),
        &need(flags, "remote")?,
        &need(flags, "onto")?,
        flags.get("push").is_some_and(|v| v == "true"),
        &specs,
    )
}
