//! Host reclaim of the shared Cargo target between an accepted writer head and its push.
use crate::disk_hygiene::Guards;
use crate::disk_hygiene::Report;
#[cfg(target_os = "linux")]
use crate::disk_measure::allocated_bytes;
#[cfg(target_os = "linux")]
use crate::disk_runtime::process_references;
use crate::disk_runtime::shared_cache_levels;
use std::{io, path::Path};

/// Reclaim the shared target from the host before the protected push, under the exclusive lease.
///
/// `Ok` when the target is under `cap` or was brought under it. `Err` carries an actionable
/// receipt when something blocks the reclaim: a target that holds a real `.git` entry or no cargo
/// marker, a lease held by live pids, or a running build. The caller keeps the accepted head and
/// retries only the push after the cause is gone.
#[cfg(target_os = "linux")]
pub fn reclaim_before_push(checkout: &Path, target: &Path, cap: u64) -> Result<Report, String> {
    reclaim_before_push_using(checkout, target, cap, process_references)
}

#[cfg(target_os = "linux")]
pub fn reclaim_before_push_using(
    checkout: &Path,
    target: &Path,
    cap: u64,
    processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> Result<Report, String> {
    let fail = |error: io::Error| format!("shared cargo target {}: {error}", target.display());
    // An over-cap target reached through a symlink (the incident layout) is measured, identified
    // and leased at its real path.
    let target = &real_target(target).map_err(fail)?;
    let size = |path: &Path| match allocated_bytes(path) {
        Ok(bytes) => Ok(bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(0),
        Err(error) => Err(fail(error)),
    };
    let before = size(target)?;
    if before <= cap {
        return Ok(Report::default());
    }
    let over = |bytes: u64| format!("{bytes} bytes against the cap of {cap} bytes");
    if !crate::shared_target::identified(target) {
        return Err(format!(
            "cargo_target_not_reclaimed: {} holds {} but is not identified as a cargo target \
             (a .git entry or no cargo marker): remove or move what is not cargo output, then retry \
             the push of the accepted head",
            target.display(),
            over(before)
        ));
    }
    let report =
        post_run_cargo_measured(checkout, target, cap, allocated_bytes, processes).map_err(fail)?;
    let after = size(target)?;
    if after > cap {
        let blockers = crate::cargo_cache::describe_holders(target);
        return Err(format!(
            "cargo_target_not_reclaimed: {} still holds {}{blockers}; skipped: [{}]: wait for the \
             running build or the holder to finish, then retry the push of the accepted head",
            target.display(),
            over(after),
            report.skipped.join("; ")
        ));
    }
    Ok(report)
}

/// The target a symlinked configuration points at: the lease file, the size and the markers all
/// belong to the real directory, and a writer's shim locks the same canonical path.
#[cfg(target_os = "linux")]
pub(crate) fn real_target(target: &Path) -> io::Result<std::path::PathBuf> {
    match target.canonicalize() {
        Ok(real) => Ok(real),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(target.to_path_buf()),
        Err(error) => Err(error),
    }
}

/// Reclaim down to the watermark and `cap`, measured with `measure`.
pub(crate) fn post_run_cargo_measured(
    checkout: &Path,
    target: &Path,
    cap: u64,
    measure: fn(&Path) -> io::Result<u64>,
    processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<Report> {
    let guards = Guards {
        pinned: vec![checkout.canonicalize()?],
        live: vec![],
    };
    let mut report = Report::default();
    shared_cache_levels(
        target,
        measure,
        &guards,
        false,
        (crate::cargo_pool::watermark_bytes(cap), cap),
        false,
        true,
        &mut report,
        processes,
    )?;
    Ok(report)
}
