//! Disk sampling and shared rebuildable-cache cleanup for Linux worker hosts.
use crate::disk_hygiene::{Guards, Report};
use crate::run::record_io;
use std::{
    fs, io,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

/// Atomically persist `disk-hygiene.json` and append a row to `disk-hygiene.ndjson`.
pub fn write_hygiene_report(state: &Path, report: &serde_json::Value) -> io::Result<Vec<u8>> {
    let mut report = report.clone();
    if report.get("ts").is_none_or(serde_json::Value::is_null) {
        report["ts"] = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_secs()
            .into();
    }
    let bytes = serde_json::to_vec(&report)?;
    record_io::atomic_write_private(&state.join("disk-hygiene.json"), &bytes)?;
    let mut line = bytes.clone();
    line.push(b'\n');
    record_io::append_private(&state.join("disk-hygiene.ndjson"), &line)?;
    Ok(bytes)
}

/// No subprocess (in particular no unbounded df invocation).
#[cfg(unix)]
pub fn disk_usage(path: &Path) -> io::Result<u64> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: path is terminated and stat points to writable storage.
    if unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful statvfs initialized stat.
    let stat = unsafe { stat.assume_init() };
    Ok(
        ((stat.f_bavail as u128).saturating_mul(stat.f_frsize as u128)).min(u64::MAX as u128)
            as u64,
    )
}
#[cfg(not(unix))]
pub fn disk_usage(_: &Path) -> io::Result<u64> {
    Err(io::Error::other("disk hygiene requires a Unix host"))
}

/// Fail closed for every visible process, regardless of proc inode owner.
/// Non-dumpable processes can have root-owned proc inodes even for this user.
pub fn processes_direct() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    scan_processes(Path::new("/proc"))
}
fn scan_processes(root: &Path) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    let mut cwds = Vec::new();
    let mut building = false;
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if !path
            .file_name()
            .unwrap()
            .to_string_lossy()
            .bytes()
            .all(|b| b.is_ascii_digit())
        {
            continue;
        }
        match fs::read_to_string(path.join("comm")) {
            Ok(name) => {
                building |= [
                    "cargo", "rustc", "rustdoc", "sccache", "cc", "gcc", "clang", "ld", "npm",
                    "make", "cmake", "ninja",
                ]
                .contains(&name.trim())
            }
            Err(e) if !vanished(&e) => return Err(e),
            _ => {}
        }
        match fs::read_link(path.join("cwd")) {
            Ok(cwd) => cwds.push(cwd),
            Err(e) if !vanished(&e) => return Err(e),
            _ => {}
        }
    }
    Ok((cwds, building))
}

pub fn processes() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    resolve_processes(processes_direct(), || {
        let reader = std::env::var_os("PG_PROCESS_CWD_READER")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| "/usr/local/libexec/pg-process-cwds".into());
        if !reader.is_absolute() {
            return Err(io::Error::other("process reader must be absolute"));
        }
        let bytes = crate::host_command::output(
            &mut std::process::Command::new(reader),
            std::time::Duration::from_secs(30),
        )?;
        let rows: (Vec<std::path::PathBuf>, bool) =
            serde_json::from_slice(&bytes).map_err(io::Error::other)?;
        if rows.0.iter().any(|p| !p.is_absolute()) {
            return Err(io::Error::other("invalid process cwd"));
        }
        Ok(rows)
    })
}
fn vanished(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::NotFound || error.raw_os_error() == Some(libc::ESRCH)
}
pub fn shared_cache(
    path: &Path,
    guards: &Guards,
    building: bool,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    shared_cache_capped(path, guards, building, 0, true, apply, report)
}
/// Above the cap, reclaim oldest idle candidates even without disk pressure.
#[allow(clippy::too_many_arguments)]
pub fn shared_cache_capped(
    path: &Path,
    guards: &Guards,
    building: bool,
    cap: u64,
    pressure: bool,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    shared_cache_using(
        path, guards, building, cap, pressure, apply, report, processes,
    )
}
#[allow(clippy::too_many_arguments)]
fn shared_cache_using(
    path: &Path,
    guards: &Guards,
    building: bool,
    cap: u64,
    pressure: bool,
    apply: bool,
    report: &mut Report,
    mut process_reader: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    if building {
        return Ok(());
    }
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(io::Error::other("unsafe shared cache"));
    }
    let canonical = path.canonicalize()?;
    if guards.protects(path) || guards.protects(&canonical) {
        return Ok(());
    }
    // Hold the sibling lease through every scan, process probe and deletion.
    // See the cross-language contract in spawner-disk-admission.md.
    let Some(_lease) = crate::cargo_cache::try_exclusive_lease(path)? else {
        report
            .skipped
            .push(format!("{}: cache lease held", path.display()));
        return Ok(());
    };
    let (cwds, building) = process_reader()?;
    if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    let mut bytes = crate::disk_hygiene::scan(path)?.0;
    let cli_tests = crate::shared_target::cli_test_candidates(path)?;
    let candidates = crate::shared_target::candidates(path, std::time::SystemTime::now())?;
    for (mandatory, (candidate, size)) in cli_tests
        .into_iter()
        .map(|c| (true, c))
        .chain(candidates.into_iter().map(|c| (false, c)))
    {
        if !mandatory && !pressure && bytes <= cap {
            break;
        }
        if guards.protects(&candidate) {
            continue;
        }
        // Recheck eligibility before the final process probe, so a potentially
        // expensive filesystem scan never separates that probe from removal.
        if apply
            && !mandatory
            && !crate::shared_target::candidates(path, std::time::SystemTime::now())?
                .iter()
                .any(|(p, _)| p == &candidate)
        {
            continue;
        }
        let (cwds, building) = process_reader()?;
        if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
            return Ok(());
        }
        if apply {
            fs::remove_dir_all(&candidate)?;
            report.freed_bytes = report.freed_bytes.saturating_add(size);
        }
        bytes = bytes.saturating_sub(size);
        report.actions.push(format!(
            "{} {} bytes={size}",
            if apply { "removed" } else { "would-remove" },
            candidate.display()
        ));
    }
    Ok(())
}

fn resolve_processes(
    direct: io::Result<(Vec<std::path::PathBuf>, bool)>,
    reader: impl FnOnce() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    match direct {
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => reader(),
        other => other,
    }
}
#[cfg(test)]
#[path = "disk_runtime_tests.rs"]
mod tests;
