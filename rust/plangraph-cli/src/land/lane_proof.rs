//! Classification of one lane head against fresh main, by whitespace-exact git proofs only.
use pg_worker_core::lane_delete::run_git;
use serde::Deserialize;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, PartialEq, Eq, Clone)]
pub(super) enum Class {
    Merged,
    /// Merging the head into main changes nothing (identical tree), so the whole branch delta is in main.
    InMain,
    Superseded(String),
    Stale,
    Live,
}

impl Class {
    pub fn label(&self) -> String {
        match self {
            Class::Merged => "merged".into(),
            Class::InMain => "in-main".into(),
            Class::Superseded(by) => format!("superseded-by:{by}"),
            Class::Stale => "stale".into(),
            Class::Live => "live".into(),
        }
    }
    pub fn deletable(&self) -> bool {
        matches!(self, Class::Merged | Class::InMain | Class::Superseded(_))
    }
}

#[derive(Deserialize, Clone)]
pub(super) struct Replacement {
    pub batch_key: String,
    pub old_ref: String,
    pub old_sha: String,
    pub new_ref: String,
    pub new_sha: String,
}

pub(super) fn ok(repo: &Path, args: &[&str]) -> Result<bool, String> {
    run_git(repo, args, 300)
        .map(|o| o.status.success())
        .map_err(|e| e.to_string())
}

pub(super) fn out(repo: &Path, args: &[&str]) -> Result<String, String> {
    let o = run_git(repo, args, 300).map_err(|e| e.to_string())?;
    if !o.status.success() {
        return Err(format!("git {} failed", args.first().unwrap_or(&"")));
    }
    Ok(String::from_utf8_lossy(&o.stdout).into_owned())
}

/// Merging `other` into `base` is clean and yields base's own tree: every change of `other`
/// (merge commits and whitespace included) is already in `base`. Patch ids are never used:
/// they ignore whitespace, which can change behaviour.
pub(super) fn adds_nothing(repo: &Path, base: &str, other: &str) -> Result<bool, String> {
    let merged = run_git(repo, &["merge-tree", "--write-tree", base, other], 300)
        .map_err(|e| e.to_string())?;
    if !merged.status.success() {
        return Ok(false);
    }
    let tree = String::from_utf8_lossy(&merged.stdout)
        .lines()
        .next()
        .unwrap_or("")
        .to_string();
    Ok(!tree.is_empty() && tree == out(repo, &["rev-parse", &format!("{base}^{{tree}}")])?.trim())
}

/// The old branch's whole delta is in the new branch or in main.
pub(super) fn preserved_by(repo: &Path, main: &str, old: &str, new: &str) -> Result<bool, String> {
    Ok(ok(repo, &["merge-base", "--is-ancestor", old, new])?
        || adds_nothing(repo, new, old)?
        || adds_nothing(repo, main, old)?)
}

pub(super) fn stem(reference: &str) -> (&str, u32) {
    match reference.rsplit_once("-r") {
        Some((stem, n)) if !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()) => {
            (stem, n.parse().unwrap_or(0))
        }
        _ => (reference, 0),
    }
}

/// `heads` are the remote heads read in this run; a record counts only while both exact refs and
/// SHAs still stand there.
pub(super) fn classify(
    repo: &Path,
    main: &str,
    stale_days: u64,
    heads: &[(String, String)],
    reference: &str,
    sha: &str,
    records: &[Replacement],
) -> Result<Class, String> {
    if !ok(repo, &["cat-file", "-e", &format!("{sha}^{{commit}}")])? {
        return Err("commit not fetched".into());
    }
    if ok(repo, &["merge-base", "--is-ancestor", sha, main])? {
        return Ok(Class::Merged);
    }
    if adds_nothing(repo, main, sha)? {
        return Ok(Class::InMain);
    }
    for rec in records
        .iter()
        .filter(|r| r.old_ref == reference && r.old_sha == sha)
    {
        let (old_stem, old_n) = stem(&rec.old_ref);
        let (new_stem, new_n) = stem(&rec.new_ref);
        let live = heads
            .iter()
            .any(|(r, s)| *r == rec.new_ref && *s == rec.new_sha);
        if live
            && old_stem == new_stem
            && new_n > old_n
            && !rec.batch_key.is_empty()
            && preserved_by(repo, main, sha, &rec.new_sha)?
        {
            return Ok(Class::Superseded(rec.new_ref.clone()));
        }
    }
    let age = out(repo, &["log", "-1", "--format=%ct", sha])?
        .trim()
        .parse::<u64>()
        .unwrap_or(0);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    Ok(if now.saturating_sub(age) > stale_days * 86400 {
        Class::Stale
    } else {
        Class::Live
    })
}
