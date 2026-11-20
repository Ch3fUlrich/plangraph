//! Leased fixed-size pool of Cargo target directories and lock-wait telemetry.
use crate::{
    cargo_cache::{try_exclusive_lease, ExclusiveLease},
    disk_hygiene::{scan, Report},
    run::record_io::{append_private, atomic_write_private},
    shared_target, sweep_runs,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub const DEFAULT_CARGO_TARGET_CAP_GB: u64 = 20;
pub const DEFAULT_POOL_SLOTS: usize = 3;
const GIB: u64 = 1024 * 1024 * 1024;
const LOCK_WAIT_LOG: &str = "cargo-pool-lock-waits.jsonl";

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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LockWaitSample {
    pub host: String,
    pub run_id: String,
    pub slot: usize,
    pub build_ms: u64,
    pub lock_wait_ms: u64,
    pub blocking_events: u64,
    pub ts: u64,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LockWaitSummary {
    pub builds: u64,
    pub build_ms: u64,
    pub lock_wait_ms: u64,
    pub blocking_events: u64,
}

impl LockWaitSummary {
    pub fn lock_wait_ratio(&self) -> f64 {
        if self.build_ms == 0 {
            0.0
        } else {
            self.lock_wait_ms as f64 / self.build_ms as f64
        }
    }
}

fn unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub fn configured_cap_bytes() -> u64 {
    let raw = std::env::var("PLANGRAPH_CARGO_TARGET_CAP_GB").ok();
    raw.as_deref()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|gb| *gb >= 0.0)
        .map(|gb| (gb * GIB as f64).round() as u64)
        .unwrap_or(DEFAULT_CARGO_TARGET_CAP_GB * GIB)
}

/// Number of pool slots that fit in free disk, clamped to `[1, default_slots]`.
pub fn slot_count(free_bytes: u64, measured_slot_bytes: u64, default_slots: usize) -> usize {
    let max_slots = default_slots.max(1);
    if measured_slot_bytes == 0 {
        return max_slots;
    }
    ((free_bytes / measured_slot_bytes) as usize).clamp(1, max_slots)
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

fn remove_path(path: &Path) -> io::Result<()> {
    if path.is_symlink() || path.is_file() {
        fs::remove_file(path)
    } else if path.is_dir() {
        fs::remove_dir_all(path)
    } else {
        Ok(())
    }
}

fn slot_size(slot: &Path) -> u64 {
    if !slot.exists() {
        0
    } else {
        scan(slot).map_or(0, |x| x.0)
    }
}

fn trim_slot_to(
    slot: &Path,
    target_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let mut size = slot_size(slot);
    if size <= target_bytes {
        return Ok(size);
    }
    let now = SystemTime::now();
    for (candidate, bytes) in shared_target::candidates(slot, now)? {
        if size <= target_bytes {
            break;
        }
        if apply {
            match remove_path(&candidate) {
                Ok(()) => {
                    report.freed_bytes += bytes;
                    report
                        .actions
                        .push(format!("pool_evict {} ({bytes}B)", candidate.display()));
                    size = size.saturating_sub(bytes);
                }
                Err(e) => report
                    .skipped
                    .push(format!("{} ({e})", candidate.display())),
            }
        } else {
            report.actions.push(format!(
                "would pool_evict {} ({bytes}B)",
                candidate.display()
            ));
            size = size.saturating_sub(bytes);
        }
    }
    if size <= target_bytes {
        return Ok(size);
    }
    // Remove idle slot profile subdirectories/files oldest-first when unit-level eviction is insufficient.
    let mut entries = vec![];
    if let Ok(read_dir) = fs::read_dir(slot) {
        for entry in read_dir {
            let entry = entry?;
            let path = entry.path();
            if path.is_symlink() {
                continue;
            }
            let (bytes, modified) = scan(&path).unwrap_or((0, UNIX_EPOCH));
            entries.push((modified, path, bytes));
        }
    }
    entries.sort_by_key(|(modified, _, _)| *modified);
    for (_, path, bytes) in entries {
        if size <= target_bytes {
            break;
        }
        if apply {
            match remove_path(&path) {
                Ok(()) => {
                    report.freed_bytes += bytes;
                    report
                        .actions
                        .push(format!("pool_trim {} ({bytes}B)", path.display()));
                    size = size.saturating_sub(bytes);
                }
                Err(e) => report.skipped.push(format!("{} ({e})", path.display())),
            }
        } else {
            report
                .actions
                .push(format!("would pool_trim {} ({bytes}B)", path.display()));
            size = size.saturating_sub(bytes);
        }
    }
    Ok(if apply { slot_size(slot) } else { size })
}

/// Keep each idle slot under `slot_cap_bytes` and total across `slots` under `N * slot_cap_bytes`.
pub fn enforce_pool_cap(
    slots: &[PathBuf],
    slot_cap_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let mut sizes = vec![0u64; slots.len()];
    let mut idle_flags = vec![false; slots.len()];
    for (idx, slot) in slots.iter().enumerate() {
        let initial = slot_size(slot);
        let Some(_lease) = try_exclusive_lease(slot)? else {
            sizes[idx] = initial;
            if initial > slot_cap_bytes {
                report.skipped.push(format!("{} (leased)", slot.display()));
            }
            continue;
        };
        idle_flags[idx] = true;
        sizes[idx] = trim_slot_to(slot, slot_cap_bytes, apply, report)?;
    }

    let total_cap = slot_cap_bytes.saturating_mul(slots.len() as u64);
    let mut total: u64 = sizes.iter().sum();
    if total > total_cap {
        let mut idle_order: Vec<usize> = (0..slots.len())
            .filter(|&i| idle_flags[i] && sizes[i] > 0)
            .collect();
        idle_order.sort_by(|&a, &b| sizes[b].cmp(&sizes[a]));
        for idx in idle_order {
            if total <= total_cap {
                break;
            }
            let Some(_lease) = try_exclusive_lease(&slots[idx])? else {
                continue;
            };
            let excess = total - total_cap;
            let desired = sizes[idx].saturating_sub(excess);
            let after = trim_slot_to(&slots[idx], desired, apply, report)?;
            total = total.saturating_sub(sizes[idx].saturating_sub(after));
            sizes[idx] = after;
        }
    }
    Ok(total)
}

pub fn count_cargo_lock_waits(stderr: &str) -> u64 {
    stderr
        .lines()
        .filter(|line| {
            line.contains("Blocking waiting for file lock on build directory")
                || line.contains("Blocking waiting for file lock on artifact directory")
        })
        .count() as u64
}

pub fn record_lock_wait(state: &Path, sample: &LockWaitSample) -> io::Result<()> {
    fs::create_dir_all(state)?;
    let mut line = serde_json::to_string(sample).map_err(io::Error::other)?;
    line.push('\n');
    append_private(&state.join(LOCK_WAIT_LOG), line.as_bytes())
}

pub fn summarize_lock_waits(state: &Path) -> io::Result<BTreeMap<String, LockWaitSummary>> {
    let path = state.join(LOCK_WAIT_LOG);
    let content = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(e),
    };
    let mut summary: BTreeMap<String, LockWaitSummary> = BTreeMap::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let sample: LockWaitSample = serde_json::from_str(trimmed).map_err(io::Error::other)?;
        let entry = summary.entry(sample.host).or_default();
        entry.builds += 1;
        entry.build_ms += sample.build_ms;
        entry.lock_wait_ms += sample.lock_wait_ms;
        entry.blocking_events += sample.blocking_events;
    }
    Ok(summary)
}
