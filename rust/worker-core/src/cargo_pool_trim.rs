//! Per-slot and whole-pool size trimming for idle Cargo target slots.
use crate::{
    cargo_cache::try_exclusive_lease,
    disk_hygiene::{scan, Report},
    shared_target,
};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

fn remove_path(path: &Path) -> io::Result<()> {
    if path.is_symlink() || path.is_file() {
        fs::remove_file(path)
    } else if path.is_dir() {
        fs::remove_dir_all(path)
    } else {
        Ok(())
    }
}

pub(crate) fn slot_size(slot: &Path) -> u64 {
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
                report.skipped.push(format!(
                    "{} (leased{})",
                    slot.display(),
                    crate::cargo_cache::describe_holders(slot)
                ));
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
