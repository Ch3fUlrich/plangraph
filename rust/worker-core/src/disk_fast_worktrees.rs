use super::{disposable_caches, git, scan, Report};
use std::{
    io,
    path::Path,
    time::{Duration, SystemTime},
};

pub(super) fn remove_old_fast_tree(
    repo: &Path,
    path: &Path,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    safe_to_remove: &impl Fn(&Path) -> io::Result<bool>,
) -> io::Result<()> {
    let (bytes, newest) = scan(path)?;
    if now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(2 * 3600) {
        return Ok(());
    }
    let _lock = crate::worktree::mutation_lock(repo).map_err(io::Error::other)?;
    if disposable_caches(path, report)?.is_none() {
        return Ok(());
    }
    if git(
        path,
        &[
            "for-each-ref",
            "--count=1",
            "--contains=HEAD",
            "--format=%(refname)",
        ],
    )?
    .trim()
    .is_empty()
    {
        report.skipped.push(format!(
            "{}: detached HEAD has no retaining ref",
            path.display()
        ));
        return Ok(());
    }
    if !safe_to_remove(path)? {
        report
            .skipped
            .push(format!("{}: active work or build", path.display()));
        return Ok(());
    }
    if apply {
        let path = path
            .to_str()
            .ok_or_else(|| io::Error::other("non-UTF8 worktree path"))?;
        crate::git::git(repo, &["worktree", "remove", "--", path]).map_err(io::Error::other)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} fast-check worktree {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        path.display(),
    ));
    Ok(())
}
