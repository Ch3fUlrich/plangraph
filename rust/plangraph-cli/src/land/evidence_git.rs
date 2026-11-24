//! The git side of evidence carry-over: the patch-id and touched paths of a batch's own diff, the paths
//! main changed since the evidence base, and the dry-run merge that decides whether a rebase is needed.
use super::evidence::{decide, Decision, Evidence, Facts, Key};
use super::git;
use std::collections::BTreeSet;
use std::io::Write;
use std::path::Path;
use std::process::Stdio;

fn paths_between(dir: &Path, from: &str, to: &str) -> Result<BTreeSet<String>, String> {
    Ok(git::out(
        dir,
        &["diff", "--name-only", "--no-renames", from, to, "--"],
    )?
    .lines()
    .map(str::to_owned)
    .collect())
}

/// `git patch-id --stable` over `from..to` as one diff.
fn patch_id(dir: &Path, from: &str, to: &str) -> Result<String, String> {
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
    let mut child = git::command(dir, None)
        .args(["patch-id", "--stable"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("git patch-id: {e}"))?;
    child
        .stdin
        .take()
        .ok_or("git patch-id: no stdin")?
        .write_all(&diff)
        .map_err(|e| format!("git patch-id: {e}"))?;
    let o = child
        .wait_with_output()
        .map_err(|e| format!("git patch-id: {e}"))?;
    if !o.status.success() {
        return Err(format!(
            "git patch-id failed: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        ));
    }
    let text = String::from_utf8_lossy(&o.stdout);
    // An empty diff has no patch-id: the key then names the empty patch.
    Ok(text
        .split_whitespace()
        .next()
        .map_or_else(|| "empty".to_owned(), str::to_owned))
}

/// The evidence key of `head` on `main`: patch-id and paths of `merge-base(main, head)..head`.
pub fn key_of(dir: &Path, main: &str, head: &str) -> Result<Key, String> {
    let fork = git::out(dir, &["merge-base", main, head])?;
    Ok(Key {
        patch_id: patch_id(dir, &fork, head)?,
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
        main_paths: paths_between(dir, &old.base, main)?,
        conflict: rebase_needed(dir, main, head)?,
        trains_now,
    };
    Ok(decide(old, &facts))
}
