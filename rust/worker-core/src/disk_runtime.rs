//! Disk sampling and shared rebuildable-cache cleanup for Linux worker hosts.
use crate::disk_hygiene::{Guards, Report};
use std::{fs, io, path::Path};

pub const FREE_FLOOR: u64 = 30_000_000_000;

/// No subprocess (in particular no unbounded df invocation).
#[cfg(unix)]
pub fn disk_usage(path: &Path) -> io::Result<(f64, u64)> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: path is terminated and stat points to writable storage.
    if unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful statvfs initialized stat.
    let stat = unsafe { stat.assume_init() };
    let total = ((stat.f_blocks as u128).saturating_mul(stat.f_frsize as u128))
        .min(u64::MAX as u128) as u64;
    let free = ((stat.f_bavail as u128).saturating_mul(stat.f_frsize as u128)).min(u64::MAX as u128)
        as u64;
    if total == 0 {
        return Err(io::Error::other("zero disk size"));
    }
    Ok((
        100.0 * total.saturating_sub(free) as f64 / total as f64,
        free,
    ))
}
#[cfg(not(unix))]
pub fn disk_usage(_: &Path) -> io::Result<(f64, u64)> {
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
    shared_cache_using(path, guards, building, apply, report, processes)
}
fn shared_cache_using(
    path: &Path,
    guards: &Guards,
    building: bool,
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
    let (cwds, building) = process_reader()?;
    if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    let bytes = crate::disk_hygiene::scan(path)?.0;
    if apply {
        let (cwds, building) = process_reader()?;
        if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
            return Ok(());
        }
        fs::remove_dir_all(path)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        path.display()
    ));
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
mod tests {
    use super::*;
    #[test]
    fn disk_hygiene_denied_cwd_needs_authoritative_reader() {
        let rows = resolve_processes(
            Err(io::Error::from(io::ErrorKind::PermissionDenied)),
            || Ok((vec!["/protected/tree".into()], false)),
        )
        .unwrap();
        assert_eq!(rows.0, vec![std::path::PathBuf::from("/protected/tree")]);
        assert!(resolve_processes(
            Err(io::Error::from(io::ErrorKind::PermissionDenied)),
            || Err(io::Error::other("reader unavailable"))
        )
        .is_err());
        let rows =
            resolve_processes(Ok((vec![], false)), || panic!("reader should not run")).unwrap();
        assert!(rows.0.is_empty());
    }
    #[test]
    fn disk_hygiene_shared_cache_apply_rechecks_builds_cwds_and_symlinks() {
        let tmp = tempfile::tempdir().unwrap();
        let cache = tmp.path().join("cargo-target");
        fs::create_dir(&cache).unwrap();
        fs::write(cache.join("object"), [0u8; 42]).unwrap();
        let mut report = Report {
            disk_pct: 92.0,
            alarm: true,
            dry_run: false,
            freed_bytes: 0,
            actions: vec![],
            skipped: vec![],
            error: None,
        };
        shared_cache_using(
            &cache,
            &Guards::default(),
            false,
            false,
            &mut report,
            || Ok((vec![cache.clone()], false)),
        )
        .unwrap();
        assert!(report.actions.is_empty());
        let mut probes = 0;
        shared_cache_using(&cache, &Guards::default(), false, true, &mut report, || {
            probes += 1;
            Ok((vec![], probes == 2))
        })
        .unwrap();
        assert!(cache.join("object").exists());
        assert_eq!(report.freed_bytes, 0);
        shared_cache_using(&cache, &Guards::default(), false, true, &mut report, || {
            Ok((vec![], false))
        })
        .unwrap();
        assert!(!cache.exists());
        assert_eq!(report.freed_bytes, 42);
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(tmp.path().join("absent"), &cache).unwrap();
            assert!(shared_cache_using(
                &cache,
                &Guards::default(),
                false,
                true,
                &mut report,
                || panic!("symlink must be refused")
            )
            .is_err());
        }
    }

    #[test]
    #[cfg(unix)]
    fn disk_hygiene_process_inventory_reads_builds_and_cwds() {
        let tmp = tempfile::tempdir().unwrap();
        let pid = tmp.path().join("123");
        fs::create_dir(&pid).unwrap();
        fs::write(pid.join("comm"), "cargo\n").unwrap();
        std::os::unix::fs::symlink("/protected/tree", pid.join("cwd")).unwrap();
        let (cwds, building) = scan_processes(tmp.path()).unwrap();
        assert!(building);
        assert_eq!(cwds, vec![std::path::PathBuf::from("/protected/tree")]);
        fs::remove_file(pid.join("cwd")).unwrap();
        fs::write(pid.join("comm"), "node\n").unwrap();
        let (cwds, building) = scan_processes(tmp.path()).unwrap();
        assert!(!building);
        assert!(cwds.is_empty());
        fs::create_dir(pid.join("cwd")).unwrap();
        assert!(scan_processes(tmp.path()).is_err());
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn disk_hygiene_denied_foreign_cwd_cannot_be_ignored() {
        use std::os::unix::fs::MetadataExt;
        // A real root-owned proc entry models the inode-owner mismatch of
        // non-dumpable processes without needing root/chown in the fixture.
        let probe = fs::read_link("/proc/1/cwd");
        if probe.is_ok() || fs::metadata("/proc/1").unwrap().uid() == unsafe { libc::geteuid() } {
            return;
        } // No foreign denied entry on privileged runners.
        assert_eq!(probe.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        let tmp = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink("/proc/1", tmp.path().join("1")).unwrap();
        let result = scan_processes(tmp.path());
        assert!(
            result.is_err(),
            "denied foreign cwd must not disappear from inventory"
        );
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    }
}
