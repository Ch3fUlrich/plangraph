//! Reclaim abandoned Cargo targets without using directory names as evidence.
use crate::disk_hygiene::{scan, Guards, Report};
use std::{
    fs, io,
    path::Path,
    time::{Duration, SystemTime},
};

fn cargo_target(path: &Path) -> bool {
    path.join(".rustc_info.json").is_file()
        && ["debug", "release"].iter().any(|p| path.join(p).is_dir())
        && !path.join("Cargo.toml").exists()
}

fn owned(path: &Path) -> io::Result<bool> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid takes no arguments and has no preconditions.
        Ok(fs::metadata(path)?.uid() == unsafe { libc::geteuid() })
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(false)
    }
}

/// Walk only two levels in the host temporary directory. Worktree targets are
/// supplied separately from Git's authoritative worktree inventory.
pub fn sweep(
    temporary: &Path,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    sweep_using(
        temporary,
        worktrees,
        guards,
        apply,
        report,
        crate::disk_runtime::processes,
    )
}

fn sweep_using(
    temporary: &Path,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    let mut targets = vec![];
    for entry in fs::read_dir(temporary)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() || !owned(&entry.path())? {
            continue;
        }
        let path = entry.path();
        if cargo_target(&path) {
            targets.push((path, None));
        } else {
            for child in fs::read_dir(path)? {
                let child = child?;
                if child.file_type()?.is_dir() && cargo_target(&child.path()) {
                    targets.push((child.path(), None));
                }
            }
        }
    }
    for tree in worktrees {
        if crate::disk_hygiene::git(tree, &["ls-files", "--", "rust/target"])?.is_empty()
            && crate::disk_hygiene::git(
                tree,
                &[
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "--",
                    "rust/target/",
                ],
            )?
            .trim()
                == "rust/target/"
        {
            let last_commit = crate::disk_hygiene::git(tree, &["log", "-1", "--format=%ct"])
                .ok()
                .and_then(|value| value.trim().parse::<u64>().ok())
                .and_then(|seconds| {
                    SystemTime::UNIX_EPOCH.checked_add(Duration::from_secs(seconds))
                });
            targets.push((tree.join("rust/target"), last_commit));
        }
    }
    for (target, last_commit) in targets {
        reclaim(&target, guards, last_commit, apply, report, &mut processes)?;
    }
    Ok(())
}

fn reclaim(
    target: &Path,
    guards: &Guards,
    last_commit: Option<SystemTime>,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    if !target.exists() || target.is_symlink() || !cargo_target(target) {
        return Ok(());
    }
    if !owned(target)? {
        return Ok(());
    }
    let canonical = target.canonicalize()?;
    // Reject symlinked parents (notably a worktree's rust directory).
    if canonical != target || guards.protects(&canonical) {
        return Ok(());
    }
    let Some(_lease) = crate::cargo_cache::try_exclusive_lease(target)? else {
        return Ok(());
    };
    let (bytes, newest) = scan(target)?;
    if last_commit.is_none_or(|commit| newest >= commit)
        && SystemTime::now().duration_since(newest).unwrap_or_default() <= Duration::from_secs(1800)
    {
        return Ok(());
    }
    let (cwds, building) = processes()?;
    if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    if apply {
        fs::remove_dir_all(target)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        target.display()
    ));
    Ok(())
}

#[cfg(all(test, unix))]
#[path = "private_targets_tests.rs"]
mod tests;
