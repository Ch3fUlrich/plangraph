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

#[cfg(target_os = "linux")]
impl crate::disk_hygiene::HygieneReport {
    pub fn sync_timing(&mut self, budget: &crate::disk_hygiene::Budget<'_>) {
        self.time_to_first_removal = budget
            .first_removal
            .get()
            .map(|at| at.duration_since(budget.start).as_secs_f64());
        self.first_removal_deadline_missed &= self
            .time_to_first_removal
            .is_none_or(|elapsed| elapsed > budget.first_for.as_secs_f64());
    }

    pub fn sample_devices(&mut self, sample: impl Fn(&Path) -> io::Result<u64>) {
        let ids: Vec<_> = self.devices.keys().copied().collect();
        for device in ids {
            let now = UNIX_EPOCH.elapsed().unwrap_or_default();
            self.sample_end(device, sample(&self.devices[&device].root), now.as_secs());
        }
    }
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

/// Process names that mark a running build.
const BUILD_TOOLS: [&str; 11] = [
    "cargo", "rustc", "rustdoc", "cc", "gcc", "clang", "ld", "npm", "make", "cmake", "ninja",
];

fn scan_process_paths(
    root: &Path,
    references: bool,
) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    let mut cwds = Vec::new();
    let mut building = false;
    for path in process_dirs(root)? {
        let path = path?;
        match fs::read_to_string(path.join("comm")) {
            Ok(name) => building |= BUILD_TOOLS.contains(&name.trim()),
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

fn process_dirs(root: &Path) -> io::Result<impl Iterator<Item = io::Result<std::path::PathBuf>>> {
    Ok(fs::read_dir(root)?.filter_map(|entry| {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => return Some(Err(error)),
        };
        let name = entry.file_name();
        let numeric = name.as_encoded_bytes().iter().all(u8::is_ascii_digit);
        numeric.then(|| Ok(entry.path()))
    }))
}

fn push_process_path(path: &Path, output: &mut Vec<std::path::PathBuf>) -> io::Result<()> {
    match fs::read_link(path) {
        Ok(target) if target.is_absolute() => output.push(target),
        Err(e) if !vanished(&e) => return Err(e),
        _ => {}
    }
    Ok(())
}

/// `PG_PROCESS_INVENTORY=reader` takes the inventory from the process reader alone, never from `/proc`:
/// fixtures that must not see the host's own builds set it; the default is the scan with the reader as
/// its fallback when `/proc` entries are unreadable.
fn reader_only() -> bool {
    std::env::var_os("PG_PROCESS_INVENTORY").is_some_and(|value| value == "reader")
}

pub fn processes() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    if reader_only() {
        return read_processes(&[]);
    }
    resolve_processes(processes_direct(), || read_processes(&[]))
}

pub fn process_references() -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    if reader_only() {
        return read_processes(&["--references"]);
    }
    resolve_processes(process_references_direct(), || {
        read_processes(&["--references"])
    })
}

fn read_processes(args: &[&str]) -> io::Result<(Vec<std::path::PathBuf>, bool)> {
    let bytes = process_reader_output(&process_reader(), args, std::time::Duration::from_secs(30))?;
    let rows: (Vec<std::path::PathBuf>, bool) =
        serde_json::from_slice(&bytes).map_err(io::Error::other)?;
    if rows.0.iter().any(|p| !p.is_absolute()) {
        return Err(io::Error::other("invalid process path"));
    }
    Ok(rows)
}

fn process_reader() -> std::path::PathBuf {
    std::env::var_os("PG_PROCESS_CWD_READER")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| "/usr/local/libexec/pg-process-cwds".into())
}

fn process_reader_output(
    reader: &Path,
    args: &[&str],
    timeout: std::time::Duration,
) -> io::Result<Vec<u8>> {
    if !reader.is_absolute() {
        return Err(io::Error::other("process reader must be absolute"));
    }
    crate::host_command::output(std::process::Command::new(reader).args(args), timeout)
}

#[cfg(target_os = "linux")]
pub use hygiene::{hygiene_references, hygiene_references_direct};

#[cfg(target_os = "linux")]
mod hygiene {
    use super::*;
    use crate::disk_hygiene::{file_identity, invalid_inventory, HygieneInventory};
    use crate::private_targets::{process_cargo_path, resolve_deleted_path};
    use crate::proc_mounts::Mount;
    use std::path::PathBuf;
    use std::{
        collections::BTreeSet,
        ffi::OsStr,
        os::unix::ffi::OsStrExt,
        time::{Duration, Instant},
    };

    fn remaining(deadline: Instant) -> io::Result<Duration> {
        deadline
            .checked_duration_since(Instant::now())
            .ok_or_else(|| io::ErrorKind::TimedOut.into())
    }

    /// Obtain hygiene evidence from the bounded proc scan or the authoritative reader. When the
    /// reader is missing or fails (an older helper without `--hygiene-references`), the inventory
    /// comes from this user's own processes and carries the reader fault in `degraded`; after a
    /// fault that is not a timeout the reader is not called again in this process.
    pub fn hygiene_references(deadline: Instant) -> io::Result<HygieneInventory> {
        static READER_FAULT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
        if reader_only() {
            return read_hygiene_processes(&process_reader(), deadline);
        }
        let reader = || match READER_FAULT.get() {
            Some(fault) => Err(io::Error::other(fault.clone())),
            None => read_hygiene_processes(&process_reader(), deadline).inspect_err(|error| {
                if error.kind() != io::ErrorKind::TimedOut {
                    let _ = READER_FAULT.set(error.to_string());
                }
            }),
        };
        // SAFETY: geteuid has no preconditions.
        let uid = unsafe { libc::geteuid() };
        resolve_hygiene(
            scan_hygiene_processes(Path::new("/proc"), deadline),
            reader,
            || scan_own_hygiene_processes(Path::new("/proc"), deadline, uid),
        )
    }

    /// Denied direct scan: the reader's inventory, else the own-process inventory marked degraded.
    pub(super) fn resolve_hygiene(
        direct: io::Result<HygieneInventory>,
        reader: impl FnOnce() -> io::Result<HygieneInventory>,
        own: impl FnOnce() -> io::Result<HygieneInventory>,
    ) -> io::Result<HygieneInventory> {
        resolve_processes(direct, || {
            reader().or_else(|fault| {
                let mut rows = own()?;
                rows.degraded = Some(format!(
                    "process reader incompatible: {fault}; inventory from own processes"
                ));
                Ok(rows)
            })
        })
    }

    /// Read the hygiene inventory in the privileged, read-only helper.
    pub fn hygiene_references_direct() -> io::Result<HygieneInventory> {
        scan_hygiene_processes(Path::new("/proc"), Instant::now() + Duration::from_secs(30))
    }

    pub(super) fn read_hygiene_processes(
        reader: &Path,
        deadline: Instant,
    ) -> io::Result<HygieneInventory> {
        let bytes = process_reader_output(
            reader,
            &["--hygiene-references"],
            remaining(deadline)?.min(Duration::from_secs(30)),
        )?;
        let rows = HygieneInventory::decode(&bytes)?;
        remaining(deadline)?;
        Ok(rows)
    }

    fn start_time(pid: &Path) -> io::Result<Option<u64>> {
        let stat = match fs::read(pid.join("stat")) {
            Ok(stat) => stat,
            Err(error) if vanished(&error) => return Ok(None),
            Err(error) => return Err(error),
        };
        let field = stat.rsplit(|b| *b == b')').next().unwrap();
        std::str::from_utf8(field)
            .ok()
            .and_then(|rest| rest.split_ascii_whitespace().nth(19)?.parse().ok())
            .map(Some)
            .ok_or_else(invalid_inventory)
    }

    /// Mount tables (theirs, ours) of a process in another mount namespace than this one.
    pub(super) type Foreign = (Vec<Mount>, Vec<Mount>);

    fn foreign_mounts(pid: &Path) -> io::Result<Option<Foreign>> {
        let theirs = match fs::read_link(pid.join("ns/mnt")) {
            Ok(namespace) => namespace,
            Err(error) if vanished(&error) => return Ok(None),
            Err(error) => return Err(error),
        };
        if fs::read_link("/proc/self/ns/mnt")? == theirs {
            return Ok(None);
        }
        let ours = crate::proc_mounts::read(Path::new("/proc/self"))?;
        Ok(Some((crate::proc_mounts::read(pid)?, ours)))
    }

    fn insert_link(
        link: &Path,
        paths: &mut BTreeSet<Vec<u8>>,
        foreign: Option<&Foreign>,
    ) -> io::Result<()> {
        let name = match fs::read_link(link) {
            Ok(name) if name.is_absolute() => name,
            Ok(_) => return Ok(()),
            Err(error) if vanished(&error) => return Ok(()),
            Err(error) => return Err(error),
        };
        let meta = match fs::metadata(link) {
            Err(error) if !vanished(&error) => return Err(error),
            result => result.ok(),
        };
        paths.extend(place(&name, meta.as_ref(), foreign)?);
        Ok(())
    }

    /// The paths in this namespace that a process reference `name` (target metadata `meta`)
    /// protects. A name from another mount namespace maps through both mount tables; an object
    /// on a device this namespace does not mount is unreachable here and protects nothing.
    pub(super) fn place(
        name: &Path,
        meta: Option<&fs::Metadata>,
        foreign: Option<&Foreign>,
    ) -> io::Result<Vec<Vec<u8>>> {
        let bytes = name.as_os_str().as_bytes();
        let deleted = bytes.strip_suffix(b" (deleted)");
        if deleted.is_some_and(|base| {
            base.starts_with(b"/memfd:") || base.starts_with(b"/SYSV") && !base[1..].contains(&b'/')
        }) {
            return Ok(vec![]);
        }
        let encode = |path: PathBuf| path.into_os_string().into_encoded_bytes();
        if let Some((theirs, ours)) = foreign {
            let base = deleted.map_or(name, |base| Path::new(OsStr::from_bytes(base)));
            let Some(actual) = meta else {
                return Ok(vec![]);
            };
            let Some(hosts) =
                crate::proc_mounts::host_paths(base, file_identity(actual).0, theirs, ours)
            else {
                return Ok(vec![]);
            };
            if hosts.is_empty() {
                return Err(invalid_inventory());
            }
            // The verified host path; else every mapped one (a path this user cannot stat, such
            // as a container overlay, lies outside every tree this user can clean).
            for host in &hosts {
                if fs::metadata(host).is_ok_and(|m| file_identity(&m) == file_identity(actual)) {
                    return Ok(vec![encode(fs::canonicalize(host)?)]);
                }
            }
            return Ok(hosts.into_iter().map(encode).collect());
        }
        if let (Some(actual), Ok(named)) = (meta, fs::canonicalize(name)) {
            if file_identity(actual) == file_identity(&fs::metadata(&named)?) {
                return Ok(vec![encode(named)]);
            }
        }
        if let Some(base) = deleted {
            let base = Path::new(OsStr::from_bytes(base));
            let base = resolve_deleted_path(base, meta.map(file_identity))?;
            return Ok(vec![bytes.to_vec(), encode(base)]);
        } else if meta.is_some() {
            return Err(invalid_inventory());
        }
        Ok(vec![])
    }

    pub(super) fn scan_hygiene_processes(
        root: &Path,
        deadline: Instant,
    ) -> io::Result<HygieneInventory> {
        let mut rows = HygieneInventory::default();
        for pid in process_dirs(root)? {
            remaining(deadline)?;
            if let Some(row) = scan_pid(&pid?, deadline)? {
                rows.merge(row);
            }
        }
        remaining(deadline)?;
        Ok(rows)
    }

    /// Real, effective, saved and filesystem uid of a process; `None` when it has vanished.
    fn process_uids(pid: &Path) -> io::Result<Option<Vec<u32>>> {
        let status = match fs::read_to_string(pid.join("status")) {
            Ok(status) => status,
            Err(error) if vanished(&error) => return Ok(None),
            Err(error) => return Err(error),
        };
        let uids = status
            .lines()
            .find_map(|line| line.strip_prefix("Uid:"))
            .and_then(|line| line.split_whitespace().map(|id| id.parse().ok()).collect())
            .filter(|uids: &Vec<u32>| !uids.is_empty());
        uids.map(Some).ok_or_else(invalid_inventory)
    }

    /// The inventory of the processes `uid` owns, readable without privileges. Other users'
    /// processes are not seen. An own process whose entries are denied (not dumpable) is skipped
    /// unless it is a build tool: then the inventory is unusable.
    pub(super) fn scan_own_hygiene_processes(
        root: &Path,
        deadline: Instant,
        uid: u32,
    ) -> io::Result<HygieneInventory> {
        let mut rows = HygieneInventory::default();
        for pid in process_dirs(root)? {
            remaining(deadline)?;
            let pid = pid?;
            if !process_uids(&pid)?.is_some_and(|uids| uids.contains(&uid)) {
                continue;
            }
            match scan_pid(&pid, deadline) {
                Ok(row) => rows.merge(row.unwrap_or_default()),
                Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
                    let name = match fs::read_to_string(pid.join("comm")) {
                        Ok(name) => name,
                        Err(error) if vanished(&error) => continue,
                        Err(error) => return Err(error),
                    };
                    if BUILD_TOOLS.contains(&name.trim()) {
                        return Err(io::Error::new(
                            io::ErrorKind::PermissionDenied,
                            format!("own build process {} unreadable", pid.display()),
                        ));
                    }
                }
                Err(error) => return Err(error),
            }
        }
        remaining(deadline)?;
        Ok(rows)
    }

    /// One process's cwd, open files, root, executable and Cargo target settings; `None` when
    /// the process vanished or its pid was reused during the read.
    fn scan_pid(pid: &Path, deadline: Instant) -> io::Result<Option<HygieneInventory>> {
        let Some(start) = start_time(pid)? else {
            return Ok(None);
        };
        let mut row = HygieneInventory::default();
        let scanned = (|| {
            let foreign = foreign_mounts(pid)?;
            let foreign = foreign.as_ref();
            insert_link(&pid.join("cwd"), &mut row.cwds, foreign)?;
            for name in ["root", "exe"] {
                insert_link(&pid.join(name), &mut row.open_paths, foreign)?;
            }
            for fd in fs::read_dir(pid.join("fd"))? {
                remaining(deadline)?;
                match fd {
                    Ok(fd) => insert_link(&fd.path(), &mut row.open_paths, foreign)?,
                    Err(error) if vanished(&error) => continue,
                    Err(error) => return Err(error),
                }
            }
            for field in fs::read(pid.join("environ"))?.split(|byte| *byte == 0) {
                remaining(deadline)?;
                if let Some(value) = field
                    .strip_prefix(b"CARGO_TARGET_DIR=")
                    .or_else(|| field.strip_prefix(b"CARGO_BUILD_TARGET_DIR="))
                    .filter(|value| !value.is_empty())
                {
                    row.cargo_targets.insert(
                        process_cargo_path(pid, Path::new(OsStr::from_bytes(value)))?
                            .into_os_string()
                            .into_encoded_bytes(),
                    );
                }
            }
            Ok(())
        })();
        if start_time(pid)? != Some(start) {
            return Ok(None);
        }
        scanned.map(|()| Some(row))
    }
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
    let (cwds, building) = process_reader()?;
    if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    // Sizes, candidates and removals all use the canonical path, the key of the dry-run projection.
    let mut bytes = match crate::disk_hygiene::scan(&canonical) {
        Ok((bytes, _)) if !apply => bytes.saturating_sub(report.projected.under(&canonical)),
        Ok((bytes, _)) => bytes,
        Err(e) => {
            crate::cargo_pool_trim::note_incomplete(report, &canonical, &e);
            return Ok(());
        }
    };
    let candidates =
        match crate::shared_target::candidates(&canonical, std::time::SystemTime::now()) {
            Ok(candidates) => candidates,
            Err(e) => {
                crate::cargo_pool_trim::note_incomplete(report, &canonical, &e);
                return Ok(());
            }
        };
    for (candidate, size) in candidates {
        if !pressure && bytes <= variant_cap {
            break;
        }
        if guards.protects(&candidate) || (!apply && report.projected.covers(&candidate)) {
            continue;
        }
        // Recheck eligibility before the final process probe, so a potentially
        // expensive filesystem scan never separates that probe from removal.
        if apply
            && !crate::shared_target::candidates(&canonical, std::time::SystemTime::now())?
                .iter()
                .any(|(p, _)| p == &candidate)
        {
            continue;
        }
        let (cwds, building) = process_reader()?;
        if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
            return Ok(());
        }
        if let Some(credit) = remove_cache_entry(&canonical, &candidate, size, apply, report) {
            bytes = bytes.saturating_sub(credit);
        }
    }
    if bytes > cap {
        let profiles = match crate::shared_target::debug_profiles(&canonical) {
            Ok(profiles) => profiles,
            Err(e) => {
                crate::cargo_pool_trim::note_incomplete(report, &canonical, &e);
                return Ok(());
            }
        };
        for (profile, size) in profiles {
            if bytes <= cap {
                break;
            }
            if guards.protects(&profile) || (!apply && report.projected.covers(&profile)) {
                continue;
            }
            let (cwds, building) = process_reader()?;
            if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
                return Ok(());
            }
            if let Some(credit) = remove_cache_entry(&canonical, &profile, size, apply, report) {
                bytes = bytes.saturating_sub(credit);
            }
        }
    }
    Ok(())
}

/// One removal of the shared cache, per unit or per whole profile, through the shared guard
/// (the cache lease is held): a refusal is a skip in a dry run as in a real one. Returns the
/// bytes credited.
fn remove_cache_entry(
    cache: &Path,
    entry: &Path,
    size: u64,
    apply: bool,
    report: &mut Report,
) -> Option<u64> {
    if let Err(e) = crate::cargo_pool_trim::remove_guarded(cache, entry, apply) {
        report.skipped.push(format!("{} ({e})", entry.display()));
        return None;
    }
    let credit = if apply {
        report.freed_bytes = report.freed_bytes.saturating_add(size);
        size
    } else {
        report.projected.project(entry, size)
    };
    report.actions.push(format!(
        "{} {} bytes={size}",
        if apply { "removed" } else { "would-remove" },
        entry.display()
    ));
    Some(credit)
}

fn resolve_processes<T>(
    direct: io::Result<T>,
    reader: impl FnOnce() -> io::Result<T>,
) -> io::Result<T> {
    match direct {
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => reader(),
        other => other,
    }
}
#[cfg(all(test, target_os = "linux"))]
#[path = "disk_runtime_fallback_tests.rs"]
mod fallback_tests;
#[cfg(test)]
#[path = "disk_runtime_tests.rs"]
mod tests;
