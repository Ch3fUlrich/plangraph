//! Leased fixed-size pool of Cargo target directories with a per-slot size cap.
use crate::{
    cargo_cache::{try_exclusive_lease, ExclusiveLease},
    disk_hygiene::Report,
    run::record_io::atomic_write_private,
    sweep_runs,
};
use serde::{Deserialize, Serialize};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub use crate::cargo_pool_telemetry::{
    count_cargo_lock_waits, host_name, record_lock_wait, summarize_lock_waits, LockWaitSample,
    LockWaitSummary, LockWaitTracker,
};

pub use crate::cargo_pool_trim::enforce_pool_cap;
use crate::cargo_pool_trim::slot_size;

/// Above the measured size of one full-workspace debug test build (25.9 GB on the
/// busiest host); a host overrides it with `cargo-target-cap-gb` in its plangraph
/// config directory or `PLANGRAPH_CARGO_TARGET_CAP_GB`.
pub const DEFAULT_CARGO_TARGET_CAP_GB: u64 = 32;
/// After each run the post-run trim evicts stale build variants down to this share of the cap.
pub const WATERMARK_PERCENT: u64 = 75;
pub const DEFAULT_POOL_SLOTS: usize = 3;
const GIB: u64 = 1024 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SlotLeaseRecord {
    pub run_id: String,
    pub pid: u32,
    pub acquired_at: u64,
}

#[derive(Debug)]
pub struct SlotLease {
    pub slot_index: usize,
    pub path: PathBuf,
    pub run_id: String,
    lease_file: PathBuf,
    _flock: ExclusiveLease,
}

impl Drop for SlotLease {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.lease_file);
    }
}

fn unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn parse_cap_gb(raw: &str) -> Option<u64> {
    raw.trim()
        .parse::<f64>()
        .ok()
        .filter(|gb| gb.is_finite() && *gb >= 0.0)
        .map(|gb| (gb * GIB as f64).round() as u64)
}

/// Host config file holding the cap in GB (one number), beside the `pgtest-required` marker.
pub fn cap_config_file() -> Option<PathBuf> {
    let root = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))?;
    Some(root.join("plangraph").join("cargo-target-cap-gb"))
}

/// The environment wins, then the host config file, then the default; an unusable value falls through.
pub fn cap_bytes_from(env: Option<&str>, file: Option<&str>) -> u64 {
    env.and_then(parse_cap_gb)
        .or_else(|| file.and_then(parse_cap_gb))
        .unwrap_or(DEFAULT_CARGO_TARGET_CAP_GB * GIB)
}

pub fn configured_cap_bytes() -> u64 {
    let raw = std::env::var("PLANGRAPH_CARGO_TARGET_CAP_GB").ok();
    let file = cap_config_file().and_then(|path| fs::read_to_string(path).ok());
    cap_bytes_from(raw.as_deref(), file.as_deref())
}

/// Level the post-run trim leaves a target at: below the cap, so the next build starts with room.
pub fn watermark_bytes(cap_bytes: u64) -> u64 {
    cap_bytes / 100 * WATERMARK_PERCENT + cap_bytes % 100 * WATERMARK_PERCENT / 100
}

/// Number of pool slots that fit in free disk, clamped to `[1, default_slots]`.
pub fn slot_count(free_bytes: u64, measured_slot_bytes: u64, default_slots: usize) -> usize {
    let max_slots = default_slots.max(1);
    if measured_slot_bytes == 0 {
        return max_slots;
    }
    ((free_bytes / measured_slot_bytes) as usize).clamp(1, max_slots)
}

/// Pool size for `primary`: how many measured slots fit in the free disk beside it,
/// at most `DEFAULT_POOL_SLOTS`; falls back to the default when free space is unknown.
pub fn configured_slot_count(primary: &Path) -> usize {
    let measured = existing_pool_slots(primary)
        .iter()
        .map(|slot| slot_size(slot))
        .max()
        .unwrap_or(0);
    let mut probe = primary;
    while !probe.exists() {
        match probe.parent() {
            Some(parent) => probe = parent,
            None => return DEFAULT_POOL_SLOTS,
        }
    }
    match crate::disk_runtime::disk_usage(probe) {
        Ok(free) => slot_count(free, measured, DEFAULT_POOL_SLOTS),
        Err(_) => DEFAULT_POOL_SLOTS,
    }
}

/// `primary` plus every `slot-*` directory currently present in its pool root.
pub fn existing_pool_slots(primary: &Path) -> Vec<PathBuf> {
    let mut slots = vec![primary.to_path_buf()];
    let Ok(root) = pool_root(primary) else {
        return slots;
    };
    let mut extra: Vec<PathBuf> = fs::read_dir(root)
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("slot-") && !n.contains('.'))
                && path.is_dir()
                && !path.is_symlink()
        })
        .collect();
    extra.sort();
    slots.extend(extra);
    slots
}

/// Trim every idle slot of the pool at `primary` to `cap_bytes` each, and the pool
/// as a whole to N x cap. Leased slots are skipped.
pub fn enforce_pool(
    primary: &Path,
    cap_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    enforce_pool_cap(&existing_pool_slots(primary), cap_bytes, apply, report)
}

pub fn pool_root(primary: &Path) -> io::Result<PathBuf> {
    let parent = primary
        .parent()
        .ok_or_else(|| io::Error::other("primary cargo target requires a parent"))?;
    let name = primary
        .file_name()
        .and_then(|s| s.to_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| io::Error::other("primary cargo target requires a directory name"))?;
    Ok(parent.join(format!("{name}-pool")))
}

/// Ordered pool slot directories: slot 0 is `primary`; `1..count` live in `<primary>-pool/slot-<i>`.
pub fn pool_slots(primary: &Path, count: usize) -> io::Result<Vec<PathBuf>> {
    let n = count.max(1);
    let mut slots = Vec::with_capacity(n);
    slots.push(primary.to_path_buf());
    if n > 1 {
        let root = pool_root(primary)?;
        for idx in 1..n {
            slots.push(root.join(format!("slot-{idx}")));
        }
    }
    Ok(slots)
}

pub fn lease_file_path(slot: &Path) -> io::Result<PathBuf> {
    let name = slot
        .file_name()
        .and_then(|s| s.to_str())
        .ok_or_else(|| io::Error::other("slot requires a directory name"))?;
    Ok(slot.with_file_name(format!("{name}.lease.json")))
}

#[cfg(unix)]
fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    // SAFETY: kill(pid, 0) checks process existence without delivering a signal.
    if unsafe { libc::kill(pid as libc::pid_t, 0) } == 0 {
        return true;
    }
    io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
}

#[cfg(not(unix))]
fn pid_alive(_: u32) -> bool {
    false
}

fn run_terminal_or_missing(state: &Path, run_id: &str) -> bool {
    if run_id.is_empty() {
        return true;
    }
    let record_path = state.join("runs").join(run_id).join("record.json");
    let Ok(bytes) = fs::read(record_path) else {
        return true;
    };
    let Ok(row) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return true;
    };
    let Some(status) = row["status"].as_str() else {
        return true;
    };
    let survivors = match row.get("survivors") {
        None => false,
        Some(serde_json::Value::Array(pids)) => !pids.is_empty(),
        Some(_) => true,
    };
    sweep_runs::terminal(status) && !survivors
}

fn stale_reason(record: Option<&SlotLeaseRecord>, state: Option<&Path>) -> bool {
    let Some(rec) = record else {
        return true;
    };
    if !pid_alive(rec.pid) {
        return true;
    }
    state.is_some_and(|s| run_terminal_or_missing(s, &rec.run_id))
}

/// Remove un-flocked lease files whose owner pid is dead or whose run record is terminal.
pub fn reap_stale_leases(slots: &[PathBuf], state: Option<&Path>) -> io::Result<usize> {
    let mut reaped = 0usize;
    for slot in slots {
        let lease_path = lease_file_path(slot)?;
        if !lease_path.exists() {
            continue;
        }
        let Some(_flock) = try_exclusive_lease(slot)? else {
            continue;
        };
        let parsed = fs::read(&lease_path)
            .ok()
            .and_then(|b| serde_json::from_slice::<SlotLeaseRecord>(&b).ok());
        if stale_reason(parsed.as_ref(), state) {
            match fs::remove_file(&lease_path) {
                Ok(()) => reaped += 1,
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
    }
    Ok(reaped)
}

/// Acquire an unused slot from `0..count`, reaping stale leases first.
pub fn acquire_slot(
    primary: &Path,
    count: usize,
    run_id: &str,
    state: Option<&Path>,
) -> io::Result<SlotLease> {
    let slots = pool_slots(primary, count)?;
    reap_stale_leases(&slots, state)?;
    for (slot_index, slot) in slots.iter().enumerate() {
        if let Some(parent) = slot.parent() {
            fs::create_dir_all(parent)?;
        }
        let Some(flock) = try_exclusive_lease(slot)? else {
            continue;
        };
        fs::create_dir_all(slot)?;
        let lease_file = lease_file_path(slot)?;
        let record = SlotLeaseRecord {
            run_id: run_id.to_owned(),
            pid: std::process::id(),
            acquired_at: unix_secs(),
        };
        let payload = serde_json::to_vec(&record).map_err(io::Error::other)?;
        atomic_write_private(&lease_file, &payload)?;
        return Ok(SlotLease {
            slot_index,
            path: slot.clone(),
            run_id: run_id.to_owned(),
            lease_file,
            _flock: flock,
        });
    }
    Err(io::Error::new(
        io::ErrorKind::WouldBlock,
        format!("all {} cargo pool slots are busy", slots.len()),
    ))
}
