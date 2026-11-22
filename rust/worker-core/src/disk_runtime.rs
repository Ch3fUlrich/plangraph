//! Disk sampling and shared rebuildable-cache cleanup for Linux worker hosts.
use crate::disk_hygiene::{Guards, Report};
use std::{fs, io, path::Path};

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
    for (candidate, size) in crate::shared_target::candidates(path, std::time::SystemTime::now())? {
        if !pressure && bytes <= cap {
            break;
        }
        if guards.protects(&candidate) {
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
