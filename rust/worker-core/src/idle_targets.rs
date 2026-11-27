//! Cargo targets of earlier layouts that nothing has written to for a day are reclaimed.
//!
//! A host once built into `~/.cache/cargo-target/<name>` and `~/.cache/pg-target-warm`; those
//! directories are outside the shared cap and kept 20 GB on a root disk. Only directories that
//! carry cargo's own markers are removed, never one a process works in, a pinned or foreign one.
use crate::{
    disk_hygiene::{scan, Guards, Report},
    private_targets::{cargo_target, owned},
};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

/// Containers of legacy targets, relative to the home directory.
pub const LEGACY_CONTAINERS: [&str; 2] = [".cache/cargo-target", ".cache/pg-target-warm"];
/// A target untouched for this long belongs to no build.
pub const IDLE_AFTER: Duration = Duration::from_secs(24 * 3600);

/// Discover known targets without walking inside one. An exhausted budget returns partial results.
pub fn targets_in(
    container: &Path,
    depth: u8,
    exclusions: &[PathBuf],
    mut permit: impl FnMut() -> bool,
) -> io::Result<(Vec<PathBuf>, bool)> {
    let mut found = vec![];
    let complete = collect_targets(container, depth, exclusions, &mut permit, &mut found)?;
    found.sort();
    Ok((found, complete))
}

fn collect_targets(
    path: &Path,
    depth: u8,
    exclusions: &[PathBuf],
    permit: &mut impl FnMut() -> bool,
    found: &mut Vec<PathBuf>,
) -> io::Result<bool> {
    if !permit() {
        return Ok(false);
    }
    if exclusions.iter().any(|root| path.starts_with(root)) {
        return Ok(true);
    }
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(true),
        Err(e) => return Err(e),
    };
    if !meta.is_dir() {
        return Ok(true);
    }
    if cargo_target(path) {
        found.push(path.to_path_buf());
    } else if depth > 0 {
        for entry in fs::read_dir(path)? {
            if !collect_targets(&entry?.path(), depth - 1, exclusions, permit, found)? {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// Private and scratch discovery must leave every shared cache and its pool to the pool policy.
pub fn hygiene_exclusions(shared: &[PathBuf], home: &Path) -> Vec<PathBuf> {
    let fallback = shared
        .first()
        .and_then(|primary| crate::cargo_cache::review_fallback_primary(primary, home));
    let mut exclusions = Vec::new();
    for target in shared.iter().chain(fallback.iter()) {
        let canonical = target.canonicalize().unwrap_or_else(|_| target.clone());
        for primary in [target, &canonical] {
            if let Ok(pool) = crate::cargo_pool::pool_root(primary) {
                exclusions.push(pool.canonicalize().unwrap_or(pool));
            }
        }
        exclusions.push(canonical);
    }
    exclusions
}

/// Report each legacy target and, with `apply`, remove the ones idle for [`IDLE_AFTER`] while no
/// cargo runs and no process works inside them.
pub fn sweep(
    containers: &[PathBuf],
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<PathBuf>, bool)>,
) -> io::Result<()> {
    for container in containers {
        for target in targets_in(container, 1, &[], || true)?.0 {
            if !owned(&target)? {
                report.skipped.push(format!(
                    "{}: legacy target owned by another user",
                    target.display()
                ));
                continue;
            }
            // Only the target itself may not be a link: a symlinked ancestor (a home reached through one)
            // is no reason to keep it, and the guards and process checks use the resolved path.
            let canonical = target.canonicalize()?;
            if fs::symlink_metadata(&target)?.file_type().is_symlink()
                || guards.protects(&canonical)
            {
                report.skipped.push(format!(
                    "{}: legacy target pinned, live or symlinked",
                    target.display()
                ));
                continue;
            }
            // The cache lease a build holds shared is taken before anything below the target is read, and held
            // through the scan and the removal.
            let Some(_lease) = crate::cargo_cache::try_exclusive_lease(&target)? else {
                report
                    .skipped
                    .push(format!("{}: cache lease held", target.display()));
                continue;
            };
            // A git repository below the target is someone's source tree, whatever markers sit beside it.
            if crate::main_target::holds_git_repository(&canonical)? {
                report.skipped.push(format!(
                    "{}: legacy target holds a git repository; not touched",
                    target.display()
                ));
                continue;
            }
            let (bytes, newest) = scan(&target)?;
            if now.duration_since(newest).unwrap_or_default() <= IDLE_AFTER {
                continue;
            }
            let (cwds, building) = processes()?;
            if building || cwds.iter().any(|cwd| cwd.starts_with(&canonical)) {
                report.skipped.push(format!(
                    "{}: legacy target in use by a running build",
                    target.display()
                ));
                continue;
            }
            if apply {
                fs::remove_dir_all(&target)?;
                report.freed_bytes = report.freed_bytes.saturating_add(bytes);
            }
            report.actions.push(format!(
                "{} {} bytes={bytes}",
                if apply { "removed" } else { "would-remove" },
                target.display()
            ));
        }
    }
    Ok(())
}

#[cfg(all(test, unix))]
#[path = "idle_targets_tests.rs"]
mod tests;
