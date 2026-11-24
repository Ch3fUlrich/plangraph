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

fn targets_in(container: &Path) -> io::Result<Vec<PathBuf>> {
    let meta = match fs::symlink_metadata(container) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e),
    };
    if !meta.is_dir() {
        return Ok(vec![]);
    }
    if cargo_target(container) {
        return Ok(vec![container.to_path_buf()]);
    }
    let mut found = vec![];
    for entry in fs::read_dir(container)? {
        let entry = entry?;
        if entry.file_type().is_ok_and(|t| t.is_dir()) && cargo_target(&entry.path()) {
            found.push(entry.path());
        }
    }
    found.sort();
    Ok(found)
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
        for target in targets_in(container)? {
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
