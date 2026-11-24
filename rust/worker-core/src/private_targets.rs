//! Reclaim abandoned Cargo targets without using directory names as evidence.
use crate::disk_hygiene::{scan, Guards, Report};
use std::{
    fs, io,
    path::Path,
    time::{Duration, SystemTime},
};

pub(crate) fn cargo_target(path: &Path) -> bool {
    path.join(".rustc_info.json").is_file()
        && ["debug", "release"].iter().any(|p| path.join(p).is_dir())
        && !path.join("Cargo.toml").exists()
}

pub(crate) fn owned(path: &Path) -> io::Result<bool> {
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

/// `Ok(None)` when the entry vanished meanwhile; any other error stays an error.
fn skip_vanished<T>(result: io::Result<T>) -> io::Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

/// Walk only two levels in the host temporary directory. Worktree targets are
/// supplied separately from Git's authoritative worktree inventory.
pub fn sweep(
    temporary: Option<&Path>,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    sweep_using(
        temporary,
        worktrees,
        guards,
        SystemTime::now(),
        apply,
        report,
        crate::disk_runtime::process_references,
    )
}

fn sweep_using(
    temporary: Option<&Path>,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    let mut targets = vec![];
    if let Some(temporary) = temporary {
        for entry in fs::read_dir(temporary)? {
            let entry = entry?;
            // Another process may remove an entry between listing and inspection.
            let Some(is_dir) = skip_vanished(entry.file_type().map(|t| t.is_dir()))? else {
                continue;
            };
            if !is_dir || !skip_vanished(owned(&entry.path()))?.unwrap_or(false) {
                continue;
            }
            let path = entry.path();
            #[cfg(target_os = "linux")]
            if crate::affected_scratch::is_run(&path) {
                skip_vanished(crate::affected_scratch::reclaim(
                    &path, guards, now, apply, report,
                ))?;
                continue;
            }
            if cargo_target(&path) {
                targets.push((path.clone(), None, path));
            } else if let Some(children) = skip_vanished(fs::read_dir(&path))? {
                for child in children {
                    let Some(child) = skip_vanished(child)? else {
                        continue;
                    };
                    if skip_vanished(child.file_type().map(|t| t.is_dir()))? == Some(true)
                        && cargo_target(&child.path())
                    {
                        targets.push((child.path(), None, child.path()));
                    }
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
            targets.push((tree.join("rust/target"), last_commit, tree.clone()));
        }
    }
    for (target, last_commit, owner) in targets {
        reclaim(
            &target,
            &owner,
            guards,
            last_commit,
            now,
            apply,
            report,
            &mut processes,
        )?;
    }
    Ok(())
}

/// Regenerable fast-check targets (`fc-*`) directly under the plangraph cache directory.
/// Cargo's own markers still identify them; the name only narrows where to look, so the
/// shared target and its pool beside them are never touched.
pub fn sweep_fast_check_targets(
    cache: &Path,
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    sweep_fast_check_targets_at(None, cache, guards, apply, report)
}

/// `proc_root` replaces `/proc` (and the external reader) for the process inventory. The
/// inventory names every working directory, open file, executable and `CARGO_TARGET_DIR`, so a
/// build running from another checkout still keeps its `fc-*` target.
pub fn sweep_fast_check_targets_at(
    proc_root: Option<&Path>,
    cache: &Path,
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    let now = SystemTime::now();
    match proc_root {
        None => sweep_fast_check_using(
            cache,
            guards,
            now,
            apply,
            report,
            crate::disk_runtime::process_references,
        ),
        Some(root) => sweep_fast_check_using(cache, guards, now, apply, report, || {
            crate::disk_runtime::process_references_at(root)
        }),
    }
}

fn sweep_fast_check_using(
    cache: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    let entries = match fs::read_dir(cache) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    for entry in entries {
        let Some(entry) = skip_vanished(entry)? else {
            continue;
        };
        let path = entry.path();
        let named = entry.file_name().to_string_lossy().starts_with("fc-");
        if named && skip_vanished(entry.file_type().map(|t| t.is_dir()))? == Some(true) {
            reclaim(
                &path,
                &path,
                guards,
                None,
                now,
                apply,
                report,
                &mut processes,
            )?;
        }
    }
    Ok(())
}

/// `owner` is the directory whose live processes make `target` busy: the worktree for its
/// `rust/target`, the target itself otherwise. A process elsewhere on the host does not.
#[allow(clippy::too_many_arguments)]
fn reclaim(
    target: &Path,
    owner: &Path,
    guards: &Guards,
    last_commit: Option<SystemTime>,
    now: SystemTime,
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
        && now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(1800)
    {
        return Ok(());
    }
    let (cwds, _) = processes()?;
    let owner = owner.canonicalize().unwrap_or_else(|_| owner.to_path_buf());
    if cwds
        .iter()
        .any(|p| p.starts_with(&canonical) || p.starts_with(&owner))
    {
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
