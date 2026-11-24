//! Job worktrees registered with the repository but outside every configured sweep root.
//!
//! A tree is removed only when nothing protects it, its tree is clean (no tracked or untracked
//! change, no ignored data except build caches), it has sat idle for a day and its head is
//! already part of `origin/main`. A pushed head that is not merged keeps its tree and is
//! reported, so no unmerged commit is ever lost.
use super::{disposable_caches, git, scan, Guards, Report};
use std::{
    io,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

const IDLE: Duration = Duration::from_secs(24 * 3600);

/// Registered worktrees (not the main one) whose path lies under none of `roots`.
pub(super) fn outside(repo: &Path, roots: &[PathBuf]) -> io::Result<Vec<PathBuf>> {
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    let mut trees = vec![];
    for (index, block) in listing.split("\0\0").filter(|s| !s.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let Some(path) = fields.iter().find_map(|s| s.strip_prefix("worktree ")) else {
            continue;
        };
        if index == 0 || fields.iter().any(|s| s.starts_with("locked")) {
            continue;
        }
        let path = Path::new(path);
        let Ok(canonical) = path.canonicalize() else {
            continue;
        };
        if std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
            || roots.iter().any(|root| canonical.starts_with(root))
        {
            continue;
        }
        trees.push(canonical);
    }
    Ok(trees)
}

pub(super) fn sweep(
    repo: &Path,
    roots: &[PathBuf],
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    safe_to_remove: &impl Fn(&Path) -> io::Result<bool>,
) -> io::Result<()> {
    for path in outside(repo, roots)? {
        if guards.protects(&path) {
            report
                .skipped
                .push(format!("{}: protected", path.display()));
            continue;
        }
        if let Err(error) = remove_if_finished(repo, &path, now, apply, report, safe_to_remove) {
            report.skipped.push(format!(
                "{}: probe/cleanup unavailable: {error}",
                path.display()
            ));
        }
    }
    Ok(())
}

fn remove_if_finished(
    repo: &Path,
    path: &Path,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    safe_to_remove: &impl Fn(&Path) -> io::Result<bool>,
) -> io::Result<()> {
    let (bytes, newest) = scan(path)?;
    if now.duration_since(newest).unwrap_or_default() <= IDLE {
        return Ok(());
    }
    if disposable_caches(path, report)?.is_none() {
        return Ok(());
    }
    if !merged(path) {
        let pushed = !git(
            path,
            &[
                "for-each-ref",
                "--count=1",
                "--contains=HEAD",
                "--format=%(refname)",
                "refs/remotes/origin/",
            ],
        )?
        .trim()
        .is_empty();
        report.skipped.push(format!(
            "{}: {} commits not in origin/main",
            path.display(),
            if pushed { "pushed" } else { "unpushed" }
        ));
        return Ok(());
    }
    if apply {
        // Everything above was read without the lock. Take it, then check protection, cleanliness
        // and ancestry again right before the removal: a run or a commit may have arrived meanwhile.
        let _lock = crate::worktree::mutation_lock(repo).map_err(io::Error::other)?;
        if !safe_to_remove(path)? {
            report
                .skipped
                .push(format!("{}: active work or build", path.display()));
            return Ok(());
        }
        if disposable_caches(path, report)?.is_none() {
            return Ok(());
        }
        if !merged(path) {
            report.skipped.push(format!(
                "{}: head left origin/main before removal",
                path.display()
            ));
            return Ok(());
        }
        let text = path
            .to_str()
            .ok_or_else(|| io::Error::other("non-UTF8 worktree path"))?;
        crate::git::git(repo, &["worktree", "remove", "--", text]).map_err(io::Error::other)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    } else if !safe_to_remove(path)? {
        report
            .skipped
            .push(format!("{}: active work or build", path.display()));
        return Ok(());
    }
    report.actions.push(format!(
        "{} stray worktree {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        path.display()
    ));
    Ok(())
}

/// The head is already part of `origin/main`.
fn merged(path: &Path) -> bool {
    git(
        path,
        &[
            "merge-base",
            "--is-ancestor",
            "HEAD",
            "refs/remotes/origin/main",
        ],
    )
    .is_ok()
}
