//! The body of the landing train's merge commit, built from the manifest and each part's seat receipt.
//!
//! One `Plan-Item: <key>` per item of every part, then one `Review-Run: <key> <run id> <part head>` per
//! item and part. The keeper's close reads these lines (`store_runs._REVIEW_RUN`); a body without a
//! `Review-Run` per `Plan-Item` leaves the close flagged `seats_missing`, so the merge refuses it.
use super::build::part_base;
use super::git;
use super::train::Manifest;
use std::collections::BTreeSet;
use std::path::Path;

/// One part with the item keys its own commits carry and the seat run that reviewed its head.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PartKeys {
    pub pr: u64,
    pub head: String,
    pub run_id: String,
    pub keys: Vec<String>,
}

fn sha40(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// A key or run id is one word: whitespace would split a trailer line the reader matches by `\S+`.
fn word(s: &str) -> bool {
    !s.is_empty() && !s.contains(char::is_whitespace)
}

/// The `Plan-Item` keys and receipt of every part of the train, in manifest order.
pub fn part_keys(repo: &Path, manifest: &Manifest) -> Result<Vec<PartKeys>, String> {
    let mut out = Vec::new();
    for part in &manifest.parts {
        let from = part_base(repo, &manifest.base, part.parent.as_ref());
        let listed = git::out(
            repo,
            &[
                "log",
                "--format=%(trailers:key=Plan-Item,valueonly,unfold)",
                &format!("{from}..{}", part.head),
            ],
        )?;
        let keys: BTreeSet<String> = listed
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(str::to_owned)
            .collect();
        if keys.is_empty() {
            return Err(format!(
                "merge body: part #{} carries no Plan-Item trailer in its own commits",
                part.pr
            ));
        }
        out.push(PartKeys {
            pr: part.pr,
            head: part.head.clone(),
            run_id: part.receipt.run_id.clone(),
            keys: keys.into_iter().collect(),
        });
    }
    Ok(out)
}

fn review_run_line(key: &str, part: &PartKeys) -> String {
    format!("Review-Run: {key} {} {}", part.run_id, part.head)
}

/// The merge body for these parts: every key once as `Plan-Item`, every (key, part) as `Review-Run`.
pub fn body_for(parts: &[PartKeys]) -> Result<String, String> {
    let mut keys: BTreeSet<&str> = BTreeSet::new();
    let mut runs = Vec::new();
    for part in parts {
        if !sha40(&part.head) || !part.run_id.starts_with("run-") || !word(&part.run_id) {
            return Err(format!(
                "merge body: part #{} has no usable head and seat run id",
                part.pr
            ));
        }
        for key in &part.keys {
            if !word(key) {
                return Err(format!(
                    "merge body: part #{} key {key:?} is not one word",
                    part.pr
                ));
            }
            keys.insert(key);
            runs.push(review_run_line(key, part));
        }
    }
    if keys.is_empty() {
        return Err("merge body: no part, so no item".into());
    }
    let mut lines: Vec<String> = keys.iter().map(|k| format!("Plan-Item: {k}")).collect();
    lines.extend(runs);
    Ok(lines.join("\n"))
}

/// The merge body of the train described by `manifest`.
pub fn merge_body(repo: &Path, manifest: &Manifest) -> Result<String, String> {
    body_for(&part_keys(repo, manifest)?)
}

/// Refuses a body that lacks a `Plan-Item` for a key of the parts or a `Review-Run` for any (key, part).
pub fn require_review_runs(body: &str, parts: &[PartKeys]) -> Result<(), String> {
    let lines: BTreeSet<&str> = body.lines().map(str::trim_end).collect();
    let mut missing = Vec::new();
    for part in parts {
        for key in &part.keys {
            if !lines.contains(format!("Plan-Item: {key}").as_str()) {
                missing.push(format!("Plan-Item: {key}"));
            }
            let want = review_run_line(key, part);
            if !lines.contains(want.as_str()) {
                missing.push(want);
            }
        }
    }
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "merge body refused: missing line(s)\n{}",
            missing.join("\n")
        ))
    }
}

/// `land-train merge-body --repo DIR --manifest FILE [--check FILE]`: prints the body; with `--check` it
/// refuses a body file that lacks any line of it.
pub(super) fn run_cli(
    flags: &std::collections::BTreeMap<String, String>,
) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let path = need("manifest")?;
    let manifest: Manifest =
        serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("{path}: {e}"))?)
            .map_err(|e| format!("{path}: {e}"))?;
    let repo = Path::new(need("repo")?);
    let parts = part_keys(repo, &manifest)?;
    if let Some(file) = flags.get("check") {
        let body = std::fs::read_to_string(file).map_err(|e| format!("{file}: {e}"))?;
        require_review_runs(&body, &parts)?;
        return Ok(vec![
            "merge body carries a Review-Run per item and part".into()
        ]);
    }
    Ok(vec![body_for(&parts)?])
}
