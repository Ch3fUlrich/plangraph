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

#[derive(Clone, Copy, Debug)]
pub struct DiskSpace {
    pub free: u64,
    pub total: u64,
}

impl DiskSpace {
    pub fn meets_floor(self, min_bytes: u64, min_percent: u8) -> bool {
        self.total > 0
            && self.free >= min_bytes
            && u128::from(self.free) * 100 >= u128::from(self.total) * u128::from(min_percent)
    }
}

pub fn disk_usage(path: &Path) -> io::Result<u64> {
    Ok(disk_space(path)?.free)
}

/// No subprocess (in particular no unbounded df invocation).
#[cfg(unix)]
pub fn disk_space(path: &Path) -> io::Result<DiskSpace> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: path is terminated and stat points to writable storage.
    if unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful statvfs initialized stat.
    let stat = unsafe { stat.assume_init() };
    let bytes = |blocks| ((blocks as u128) * (stat.f_frsize as u128)).min(u64::MAX as u128) as u64;
    Ok(DiskSpace {
        free: bytes(stat.f_bavail),
        total: bytes(stat.f_blocks),
    })
}
#[cfg(not(unix))]
pub fn disk_space(_: &Path) -> io::Result<DiskSpace> {
    Err(io::Error::other("disk hygiene requires a Unix host"))
}

/// Fail closed for every visible process, regardless of proc inode owner.
/// Non-dumpable processes can have root-owned proc inodes even for this user.
pub fn processes_direct() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    scan_process_paths(Path::new("/proc"), false)
}
/// Includes files and executables even when a process has changed its working directory.
pub fn process_references_direct() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    scan_process_paths(Path::new("/proc"), true)
}

fn scan_process_paths(
    root: &Path,
    references: bool,
) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
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
                    "cargo", "rustc", "rustdoc", "cc", "gcc", "clang", "ld", "npm", "make",
                    "cmake", "ninja",
                ]
                .contains(&name.trim())
            }
            Err(e) if !vanished(&e) => return Err(e),
            _ => {}
        }
        push_process_path(&path.join("cwd"), &mut cwds)?;
        if references {
            push_cargo_target_env(&path.join("environ"), &mut cwds)?;
            push_process_path(&path.join("exe"), &mut cwds)?;
            push_process_path(&path.join("root"), &mut cwds)?;
            match fs::read_dir(path.join("fd")) {
                Ok(fds) => {
                    for fd in fds {
                        match fd {
                            Ok(fd) => push_process_path(&fd.path(), &mut cwds)?,
                            Err(e) if !vanished(&e) => return Err(e),
                            Err(_) => {}
                        }
                    }
                }
                Err(e) if !vanished(&e) => return Err(e),
                Err(_) => {}
            }
        }
    }
    Ok((cwds, building))
}

/// A running build names its target in `CARGO_TARGET_DIR`; that directory is in use even when
/// the process works elsewhere.
fn push_cargo_target_env(environ: &Path, output: &mut Vec<std::path::PathBuf>) -> io::Result<()> {
    match fs::read(environ) {
        Ok(bytes) => {
            for entry in bytes.split(|b| *b == 0) {
                let value = entry.strip_prefix(b"CARGO_TARGET_DIR=");
                if let Some(target) = value.and_then(|v| std::str::from_utf8(v).ok()) {
                    let target = std::path::PathBuf::from(target);
                    if target.is_absolute() {
                        output.push(target);
                    }
                }
            }
        }
        Err(e) if !vanished(&e) => return Err(e),
        _ => {}
    }
    Ok(())
}

fn push_process_path(path: &Path, output: &mut Vec<std::path::PathBuf>) -> io::Result<()> {
    match fs::read_link(path) {
        Ok(target) if target.is_absolute() => output.push(target),
        Err(e) if !vanished(&e) => return Err(e),
        _ => {}
    }
    Ok(())
}

pub fn processes() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    resolve_processes(processes_direct(), || read_processes(&[]))
}

/// The reference inventory of one process tree root (`/proc` in production), without the
/// external reader fallback.
pub fn process_references_at(root: &Path) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    scan_process_paths(root, true)
}

pub fn process_references() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    resolve_processes(process_references_direct(), || {
        read_processes(&["--references"])
    })
}

fn read_processes(args: &[&str]) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    let reader = std::env::var_os("PG_PROCESS_CWD_READER")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| "/usr/local/libexec/pg-process-cwds".into());
    if !reader.is_absolute() {
        return Err(io::Error::other("process reader must be absolute"));
    }
    let bytes = crate::host_command::output(
        std::process::Command::new(reader).args(args),
        std::time::Duration::from_secs(30),
    )?;
    let rows: (Vec<std::path::PathBuf>, bool) =
        serde_json::from_slice(&bytes).map_err(io::Error::other)?;
    if rows.0.iter().any(|p| !p.is_absolute()) {
        return Err(io::Error::other("invalid process path"));
    }
    Ok(rows)
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

/// Best-effort cap enforcement after a train check or writer has released its build lease.
pub fn post_run_cargo(checkout: &Path) -> io::Result<Report> {
    let target = crate::cargo_cache::shared_cache_roots()?.remove(0);
    post_run_cargo_using(
        checkout,
        &target,
        crate::cargo_pool::configured_cap_bytes(),
        process_references,
    )
}

fn post_run_cargo_using(
    checkout: &Path,
    target: &Path,
    cap: u64,
    processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<Report> {
    let guards = Guards {
        pinned: vec![checkout.canonicalize()?],
        live: vec![],
    };
    let mut report = Report::default();
    shared_cache_levels(
        target,
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
        path,
        guards,
        building,
        cap,
        pressure,
        apply,
        report,
        process_references,
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
    process_reader: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    shared_cache_levels(
        path,
        guards,
        building,
        (cap, cap),
        pressure,
        apply,
        report,
        process_reader,
    )
}

/// `levels` = (stale build variants go until the target is under .0, whole debug profiles only above .1).
#[allow(clippy::too_many_arguments)]
fn shared_cache_levels(
    path: &Path,
    guards: &Guards,
    building: bool,
    (variant_cap, cap): (u64, u64),
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
    #[cfg(unix)]
    if !crate::private_targets::owned(path)? {
        return Err(io::Error::other("shared cache has a different owner"));
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
    let (cwds, _) = process_reader()?;
    if cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    let mut bytes = crate::disk_hygiene::scan(path)?.0;
    let mut projected = Vec::new();
    for (candidate, size) in crate::shared_target::candidates(path, std::time::SystemTime::now())? {
        if !pressure && bytes <= variant_cap {
            break;
        }
        if guards.protects(&candidate) {
            continue;
        }
        #[cfg(target_os = "linux")]
        if crate::reap_journal::check_mounts(&candidate.canonicalize()?).is_err() {
            continue;
        }
        // Recheck eligibility before the final process probe, so a potentially
        // expensive filesystem scan never separates that probe from removal.
        if apply
            && !crate::shared_target::candidates(path, std::time::SystemTime::now())?
                .iter()
                .any(|(p, _)| p == &candidate)
        {
            continue;
        }
        let (cwds, _) = process_reader()?;
        if cwds.iter().any(|p| p.starts_with(&canonical)) {
            return Ok(());
        }
        if apply {
            fs::remove_dir_all(&candidate)?;
            report.freed_bytes = report.freed_bytes.saturating_add(size);
        }
        if !apply {
            projected.push((candidate.clone(), size));
        }
        bytes = bytes.saturating_sub(size);
        report.actions.push(format!(
            "{} {} bytes={size}",
            if apply { "removed" } else { "would-remove" },
            candidate.display()
        ));
    }
    if bytes > cap {
        for (profile, mut size) in crate::shared_target::debug_profiles(path)? {
            if bytes <= cap {
                break;
            }
            if guards.protects(&profile) {
                continue;
            }
            let (cwds, _) = process_reader()?;
            if cwds.iter().any(|p| p.starts_with(&canonical)) {
                return Ok(());
            }
            if apply {
                fs::remove_dir_all(&profile)?;
                report.freed_bytes = report.freed_bytes.saturating_add(size);
            } else {
                for (entry, removed) in &projected {
                    if entry.starts_with(&profile) {
                        size = size.saturating_sub(*removed);
                    }
                }
            }
            bytes = bytes.saturating_sub(size);
            report.actions.push(format!(
                "{} {} bytes={size}",
                if apply { "removed" } else { "would-remove" },
                profile.display()
            ));
        }
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
