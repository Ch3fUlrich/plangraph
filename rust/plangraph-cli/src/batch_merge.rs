//! Rule 3.39 decision over caller-collected, head-bound evidence; no merge side effects.
use chrono::{DateTime, FixedOffset};
use serde::Deserialize;
use std::{collections::BTreeSet, path::Path, process::Command};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ready {
    pub pr: u64,
    pub head: String,
    pub review: String,
    pub fast_green: bool,
    pub ready_at: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Batch {
    pub pr: u64,
    pub head: String,
    pub pushed_at: String,
    pub parts: Vec<Ready>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Facts {
    pub pr: u64,
    pub head: String,
    pub go_row: String,
    /// Complete open-PR inventory, excluding the batch PR itself.
    pub ready: Vec<Ready>,
    pub batch: Option<Batch>,
}

fn sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn time(value: &str) -> Result<DateTime<FixedOffset>, String> {
    DateTime::parse_from_rfc3339(value).map_err(|_| "invalid_evidence: timestamp".into())
}

fn validate(prs: &[Ready]) -> Result<(), String> {
    let mut ids = BTreeSet::new();
    for pr in prs {
        if pr.pr == 0 || !sha(&pr.head) || !ids.insert(pr.pr) {
            return Err("invalid_evidence: PR number, head or duplicate".into());
        }
        if !["ACCEPT", "REQUEST_CHANGES", "PENDING"].contains(&pr.review.as_str()) {
            return Err("invalid_evidence: review".into());
        }
        time(&pr.ready_at)?;
    }
    Ok(())
}

fn ready(pr: &&Ready) -> bool {
    pr.review == "ACCEPT" && pr.fast_green
}

pub fn check(facts: &Facts, repo: &Path) -> Result<String, String> {
    if facts.pr == 0 || !sha(&facts.head) {
        return Err("invalid_evidence: target".into());
    }
    validate(&facts.ready)?;
    let tokens: Vec<_> = facts.go_row.split_whitespace().collect();
    let pr_token = facts.pr.to_string();
    if facts.go_row.lines().count() != 1
        || tokens.get(0..3) != Some(&["GO", pr_token.as_str(), facts.head.as_str()])
        || tokens.len() > 4
        || (tokens.len() == 4 && !tokens[3].starts_with("EXCEPTION:"))
    {
        return Err("invalid_go: row must name GO, exact PR number and full head".into());
    }
    let current: Vec<_> = facts.ready.iter().filter(ready).collect();
    if let Some(batch) = &facts.batch {
        if tokens.len() != 3 {
            return Err("invalid_go: batch row must not name a single-PR exception".into());
        }
        if batch.pr != facts.pr || batch.head != facts.head || batch.parts.is_empty() {
            return Err("invalid_batch: target or empty parts".into());
        }
        let cutoff = time(&batch.pushed_at)?;
        validate(&batch.parts)?;
        for part in &batch.parts {
            if !ready(&part) || part.pr == batch.pr || time(&part.ready_at)? > cutoff {
                return Err("invalid_batch: part was not ready at push".into());
            }
            let result = Command::new("git")
                .current_dir(repo)
                .args(["merge-base", "--is-ancestor", &part.head, &facts.head])
                .output()
                .map_err(|e| format!("invalid_batch: ancestry unavailable: {e}"))?;
            if !result.status.success() {
                return Err(format!("invalid_batch: PR #{} head not contained", part.pr));
            }
        }
        for pr in current {
            if pr.pr == batch.pr {
                return Err("invalid_evidence: batch PR included in part inventory".into());
            }
            if time(&pr.ready_at)? <= cutoff
                && !batch
                    .parts
                    .iter()
                    .any(|p| p.pr == pr.pr && p.head == pr.head)
            {
                return Err(format!(
                    "batch_required: PR #{} was ready before batch push",
                    pr.pr
                ));
            }
        }
        return Ok(format!(
            "GO {} {} parts={}",
            facts.pr,
            facts.head,
            batch
                .parts
                .iter()
                .map(|p| p.pr.to_string())
                .collect::<Vec<_>>()
                .join(",")
        ));
    }
    if !current
        .iter()
        .any(|p| p.pr == facts.pr && p.head == facts.head)
    {
        return Err("invalid_evidence: target is not GO-ready at this head".into());
    }
    let exceptions: Vec<_> = tokens
        .iter()
        .filter(|t| t.starts_with("EXCEPTION:"))
        .collect();
    let exception = match exceptions.as_slice() {
        [token] if ["EXCEPTION:main-red-fix", "EXCEPTION:operator-order"].contains(token) => **token,
        [token] if **token == "EXCEPTION:only-one-ready-pr" && current.len() == 1 => **token,
        _ => return Err("batch_required: use one batch and one full CI; single PR needs a valid named GO exception".into()),
    };
    Ok(format!("GO {} {} {exception}", facts.pr, facts.head))
}

pub fn run(path: &Path) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("invalid_evidence: {e}"))?;
    let facts: Facts =
        serde_json::from_slice(&bytes).map_err(|e| format!("invalid_evidence: {e}"))?;
    check(&facts, Path::new("."))
}
