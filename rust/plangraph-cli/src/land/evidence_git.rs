//! The git side of evidence carry-over: the patch-id and touched paths of a batch's own diff, the paths
//! main changed since the evidence base, and the dry-run merge that decides whether a rebase is needed.
use super::evidence::{decide, Decision, Evidence, Facts, Key};
use super::git;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::Path;

fn paths_between(dir: &Path, from: &str, to: &str) -> Result<BTreeSet<String>, String> {
    Ok(git::out(
        dir,
        &["diff", "--name-only", "--no-renames", from, to, "--"],
    )?
    .lines()
    .map(str::to_owned)
    .collect())
}

/// Paths any commit of `from..to` touched, merges included: a path touched and reverted still counts,
/// which two endpoint trees would miss.
fn paths_touched_since(dir: &Path, from: &str, to: &str) -> Result<BTreeSet<String>, String> {
    let range = format!("{from}..{to}");
    Ok(git::out(
        dir,
        &[
            "log",
            "-m",
            "--no-renames",
            "--name-only",
            "--format=",
            "--end-of-options",
            &range,
            "--",
        ],
    )?
    .lines()
    .filter(|l| !l.is_empty())
    .map(str::to_owned)
    .collect())
}

/// The identity of the diff `from..to` that keeps every byte of content, whitespace included (`git
/// patch-id --stable` strips whitespace). Only the line numbers of the hunk headers and the blob ids of the `index` lines are dropped: they
/// move when main changes lines above the patch, and the paths rule covers what they would catch.
fn patch_identity(dir: &Path, from: &str, to: &str) -> Result<String, String> {
    let diff = git::bytes(
        dir,
        &[
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            from,
            to,
            "--",
        ],
    )?;
    let mut hash = Sha256::new();
    for line in diff.split_inclusive(|&b| b == b'\n') {
        // The blob ids of an `index` line change with whatever main did to the file before the patch.
        if line.starts_with(b"index ") {
            continue;
        }
        match line.strip_prefix(b"@@ ") {
            Some(rest) => match rest.windows(2).position(|w| w == b"@@") {
                Some(end) => {
                    hash.update(b"@@");
                    hash.update(&rest[end + 2..]);
                }
                None => hash.update(line),
            },
            None => hash.update(line),
        }
    }
    Ok(format!("{:x}", hash.finalize()))
}

/// The evidence key of `head` on `main`: patch-id and paths of `merge-base(main, head)..head`.
pub fn key_of(dir: &Path, main: &str, head: &str) -> Result<Key, String> {
    let fork = git::out(dir, &["merge-base", main, head])?;
    Ok(Key {
        patch_id: patch_identity(dir, &fork, head)?,
        paths: paths_between(dir, &fork, head)?,
    })
}

/// A merge-tree dry run of `head` onto `main`: a rebase is needed only when this conflicts textually.
pub fn rebase_needed(dir: &Path, main: &str, head: &str) -> Result<bool, String> {
    let o = git::run(
        dir,
        None,
        &["merge-tree", "--write-tree", "--no-messages", main, head],
    )?;
    match o.status.code() {
        Some(0) => Ok(false),
        Some(1) => Ok(true),
        _ => Err(format!(
            "merge-tree: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        )),
    }
}

/// The facts for `head` (rebased or not) on `main`, and the decision on the old evidence.
pub fn assess(
    dir: &Path,
    old: &Evidence,
    main: &str,
    head: &str,
    trains_now: u64,
) -> Result<Decision, String> {
    let facts = Facts {
        key_now: key_of(dir, main, head)?,
        new_base: main.to_owned(),
        new_head: head.to_owned(),
        main_paths: paths_touched_since(dir, &old.base, main)?,
        conflict: rebase_needed(dir, main, head)?,
        trains_now,
    };
    Ok(decide(old, &facts))
}
