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
    permit: impl FnMut() -> bool,
    keep: impl FnMut(&Path, &'static str),
) -> io::Result<(Vec<PathBuf>, bool)> {
    let mut found = vec![];
    let complete = TargetScan {
        exclusions,
        permit,
        keep,
        found: &mut found,
    }
    .visit(container, depth, true)?;
    found.sort();
    Ok((found, complete))
}

struct TargetScan<'a, P, K> {
    exclusions: &'a [PathBuf],
    permit: P,
    keep: K,
    found: &'a mut Vec<PathBuf>,
}

impl<P: FnMut() -> bool, K: FnMut(&Path, &'static str)> TargetScan<'_, P, K> {
    fn visit(&mut self, path: &Path, depth: u8, container: bool) -> io::Result<bool> {
        if !(self.permit)() {
            return Ok(false);
        }
        if self.exclusions.iter().any(|root| path.starts_with(root)) {
            return Ok(true);
        }
        let Some(meta) = discovery_probe(path.symlink_metadata(), path, &mut self.keep)? else {
            return Ok(true);
        };
        if !meta.is_dir() {
            return Ok(true);
        }
        let Some(owner) = discovery_probe(owned(path), path, &mut self.keep)? else {
            return Ok(true);
        };
        let owner = !cfg!(unix) || owner;
        if !owner && !container {
            (self.keep)(path, "unsafe");
        } else if owner && cargo_target(path) {
            self.found.push(path.to_path_buf());
        } else if depth > 0 {
            let before = self.found.len();
            let Some(entries) = discovery_probe(path.read_dir(), path, &mut self.keep)? else {
                return Ok(true);
            };
            for entry in entries {
                let Some(entry) = discovery_probe(entry, path, &mut self.keep)? else {
                    continue;
                };
                if !self.visit(&entry.path(), depth - 1, false)? {
                    return Ok(false);
                }
            }
            if depth == 1 && self.found.len() == before {
                (self.keep)(path, "unrecognized");
            }
        }
        Ok(true)
    }
}

pub(crate) fn discovery_probe<T>(
    result: io::Result<T>,
    path: &Path,
    keep: &mut impl FnMut(&Path, &'static str),
) -> io::Result<Option<T>> {
    let error = match result {
        Ok(value) => return Ok(Some(value)),
        Err(error) => error,
    };
    let reason = match error.kind() {
        io::ErrorKind::NotFound => "vanished",
        io::ErrorKind::PermissionDenied => "probe_unavailable",
        _ => return Err(error),
    };
    keep(path, reason);
    Ok(None)
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
        let keep = |path: &Path, reason| {
            if reason != "unrecognized" {
                report.skipped.push(format!("{}: {reason}", path.display()));
            }
        };
        for target in targets_in(container, 1, &[], || true, keep)?.0 {
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
