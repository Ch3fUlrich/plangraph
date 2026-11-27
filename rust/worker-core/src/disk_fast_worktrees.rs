use super::{disposable_caches, git, scan, Report};
use std::{
    io,
    path::Path,
    time::{Duration, SystemTime},
};

#[cfg(target_os = "linux")]
pub fn registered(
    repo: &Path,
    time: Duration,
) -> io::Result<Vec<(std::path::PathBuf, bool, bool)>> {
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt, process::Command};
    let output = crate::host_command::output(
        Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["worktree", "list", "--porcelain", "-z"]),
        time,
    )?;
    let mut rows = Vec::new();
    let mut fields = output.split(|byte| *byte == 0);
    while let Some(field) = fields.next().filter(|field| !field.is_empty()) {
        let Some(path) = field.strip_prefix(b"worktree ") else {
            return Err(io::Error::other("invalid Git worktree listing"));
        };
        let path = std::path::PathBuf::from(OsStr::from_bytes(path));
        let (mut locked, mut detached) = (false, false);
        for field in fields.by_ref().take_while(|field| !field.is_empty()) {
            locked |= field == b"locked" || field.starts_with(b"locked ");
            detached |= field == b"detached";
        }
        rows.push((path, locked, detached));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(rows)
}

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
