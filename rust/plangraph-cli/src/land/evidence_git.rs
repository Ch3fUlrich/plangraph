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

/// Paths any commit of `from..to` touched: every commit that brought a change in, and a merge by what it
/// changed against all its parents (a conflict resolution, an evil merge): what a merge merely carries over
/// from one side, main's own history or a side branch that synced main, is not a touch of its own. A path
/// touched and reverted still counts, which two endpoint trees would miss.
fn paths_touched_since(dir: &Path, from: &str, to: &str) -> Result<BTreeSet<String>, String> {
    let range = format!("{from}..{to}");
    let list = |kind: &str, diff: &str| {
        git::out(
            dir,
            &[
                "log",
                kind,
                diff,
                "--no-renames",
                "--name-only",
                "--format=",
                "--end-of-options",
                &range,
                "--",
            ],
        )
    };
    let commits = list("--no-merges", "--diff-merges=off")?;
    // A merge off the mainline counts by what it changed against all its parents; a merge on main's own
    // line counts by what it changed against main (its first parent), so a merge that takes a side
    // parent's version of a file still counts while a side branch that synced main inherits nothing.
    let nested = list("--merges", "--diff-merges=dense-combined")?;
    let mainline = git::out(
        dir,
        &[
            "log",
            "--first-parent",
            "--merges",
            "--diff-merges=first-parent",
            "--no-renames",
            "--name-only",
            "--format=",
            "--end-of-options",
            &range,
            "--",
        ],
    )?;
    Ok(commits
        .lines()
        .chain(nested.lines())
        .chain(mainline.lines())
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

/// The evidence key of a part's own diff: patch-id and paths of `merge-base(main, head)..head`. A part stacked on an unlanded parent is diffed against that parent's head,
/// the registrar's part diff base, so a change of the parent alone does not change the child's key.
pub fn key_on(dir: &Path, main: &str, head: &str, parent: Option<&str>) -> Result<Key, String> {
    let fork = match parent {
        Some(p) => p.to_owned(),
        None => git::out(dir, &["merge-base", main, head])?,
    };
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
    parent: Option<&str>,
    trains_now: u64,
) -> Result<Decision, String> {
    let facts = Facts {
        key_now: key_on(dir, main, head, parent)?,
        new_base: main.to_owned(),
        new_head: head.to_owned(),
        main_paths: paths_touched_since(dir, &old.base, main)?,
        conflict: rebase_needed(dir, main, head)?,
        trains_now,
    };
    Ok(decide(old, &facts))
}
