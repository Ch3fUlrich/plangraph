//! Per-slot and whole-pool size trimming for idle Cargo target slots.
use crate::{
    cargo_cache::try_exclusive_lease,
    disk_hygiene::{scan, Report},
    shared_target,
};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::SystemTime,
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

/// Refuses a path with a mount point at it, beneath it or between `boundary` and it. The mount
/// inventory is read now; one that cannot be read refuses too, so bytes stay when unsure. Only
/// Linux lists mounts, the platform the shared Cargo targets live on.
pub(crate) fn mount_guard(boundary: &Path, path: &Path) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    return crate::reap_journal::check_mounts_below(boundary, path);
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (boundary, path);
        Ok(())
    }
}

/// The one deletion boundary of Cargo pools and caches. `path` must resolve to a strict
/// descendant of `boundary` (the validated slot or cache, held under its lease) and no mount may
/// lie on the way or inside it. Without `apply` it only runs those checks, so a dry run names the
/// same refusals as the real run. The resolved path is the one removed, never the one asked for.
pub(crate) fn remove_guarded(boundary: &Path, path: &Path, apply: bool) -> io::Result<()> {
    let boundary = boundary.canonicalize()?;
    let (Some(parent), Some(name)) = (path.parent(), path.file_name()) else {
        return Err(io::Error::other("candidate has no parent"));
    };
    let resolved = parent.canonicalize()?.join(name);
    if resolved == boundary || !resolved.starts_with(&boundary) {
        return Err(io::Error::other("candidate outside the trusted slot"));
    }
    mount_guard(&boundary, &resolved)?;
    if apply {
        remove_path(&resolved)?;
    }
    Ok(())
}

#[cfg(target_os = "linux")]
pub(crate) use artifact_walk::{stamp, ArtifactWalk};

#[cfg(target_os = "linux")]
mod artifact_walk {
    use super::*;
    use crate::main_target::GitMarkers;
    use std::{collections::HashMap, os::unix::fs::MetadataExt};

    type Stamp = (u64, u64, i64, i64);
    pub(crate) fn stamp(meta: &fs::Metadata) -> Stamp {
        (meta.dev(), meta.ino(), meta.ctime(), meta.ctime_nsec())
    }

    #[derive(Default)]
    pub(crate) struct ArtifactWalk {
        dirs: Vec<(fs::ReadDir, GitMarkers)>,
        seen: Vec<(PathBuf, Stamp)>,
        links: HashMap<u64, u64>,
        pub(crate) bytes: u64,
        pub(crate) credit: u64,
        pub(crate) complete: bool,
    }

    impl ArtifactWalk {
        pub(crate) fn root_stamp(&self) -> Option<Stamp> {
            self.seen.first().map(|(_, stamp)| *stamp)
        }

        pub(crate) fn unchanged(&self, room: impl Fn() -> bool) -> Option<bool> {
            for (path, observed) in &self.seen {
                if !room() {
                    return None;
                }
                if !fs::symlink_metadata(path).is_ok_and(|meta| stamp(&meta) == *observed) {
                    return Some(false);
                }
            }
            Some(true)
        }

        pub(crate) fn step(
            &mut self,
            root: &Path,
            cutoff: SystemTime,
            dev: u64,
            uid: u32,
        ) -> Result<(), &'static str> {
            let path = if self.seen.is_empty() {
                root.to_path_buf()
            } else {
                loop {
                    let Some((entries, _)) = self.dirs.last_mut() else {
                        self.complete = true;
                        return Ok(());
                    };
                    match entries.next() {
                        Some(Ok(entry)) => break entry.path(),
                        Some(Err(_)) => return Err("probe_unavailable"),
                        None => {
                            self.dirs.pop();
                        }
                    }
                }
            };
            let meta = fs::symlink_metadata(&path).map_err(|error| match error.kind() {
                io::ErrorKind::NotFound => "vanished",
                _ => "probe_unavailable",
            })?;
            let kind = meta.file_type();
            if meta.dev() != dev
                || meta.uid() != uid
                || !(kind.is_dir() || kind.is_file() || kind.is_symlink())
            {
                return Err("unsafe");
            }
            if let Some((_, git)) = self.dirs.last_mut() {
                git.observe(path.file_name().unwrap(), kind);
                if git.is_repository() {
                    return Err("unsafe");
                }
            }
            if meta.modified().map_err(|_| "probe_unavailable")? >= cutoff {
                return Err("young");
            }
            let bytes = meta.blocks().saturating_mul(512);
            let (first, last) = if !kind.is_dir() && meta.nlink() > 1 {
                let link = self.links.entry(meta.ino()).or_default();
                *link += 1;
                (*link == 1, *link == meta.nlink())
            } else {
                (true, true)
            };
            self.bytes = self.bytes.saturating_add(if first { bytes } else { 0 });
            self.credit = self.credit.saturating_add(if last { bytes } else { 0 });
            self.seen.push((path.clone(), stamp(&meta)));
            if kind.is_dir() {
                let entries = fs::read_dir(path).map_err(|_| "probe_unavailable")?;
                self.dirs.push((entries, GitMarkers::default()));
            }
            Ok(())
        }
    }
}

/// A size that cannot be measured is skipped and the run alarms: the totals it feeds are a lower
/// bound, never a complete-looking number.
pub(crate) fn note_incomplete(report: &mut Report, path: &Path, error: &io::Error) {
    report.skipped.push(format!(
        "{} (size unknown: {error}; total incomplete)",
        path.display()
    ));
    report.alarm = true;
}

/// Bytes under `slot`; a missing one holds none.
pub(crate) fn slot_size(slot: &Path) -> io::Result<u64> {
    match scan(slot) {
        Ok((bytes, _)) => Ok(bytes),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(0),
        Err(e) => Err(e),
    }
}

/// Bytes `slot` holds that no dry run has projected away yet; `None` once the incompleteness
/// is reported.
fn retained(slot: &Path, report: &mut Report) -> Option<u64> {
    match slot_size(slot) {
        Ok(bytes) => Some(bytes.saturating_sub(report.projected.under(slot))),
        Err(e) => {
            note_incomplete(report, slot, &e);
            None
        }
    }
}

/// Real directory `slot` of the pool of `primary`: the primary, or a direct `slot-*` child of its
/// pool root, neither a symlink (ancestors may be). `None` when it does not exist.
fn trusted_slot(primary: &Path, slot: &Path) -> io::Result<Option<PathBuf>> {
    let meta = match fs::symlink_metadata(slot) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    };
    if !meta.is_dir() {
        return Err(io::Error::other("slot is not a real directory"));
    }
    if slot != primary {
        let root = crate::cargo_pool::pool_root(primary)?;
        let named = slot
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("slot-") && !n.contains('.'));
        if slot.parent() != Some(root.as_path()) || !named {
            return Err(io::Error::other("not a direct slot of the pool"));
        }
        if !fs::symlink_metadata(&root)?.is_dir() {
            return Err(io::Error::other("pool root is not a real directory"));
        }
    }
    slot.canonicalize().map(Some)
}

/// Removes (or, in a dry run, projects) `path` through the guard; the bytes it credits, or
/// `None` with the refusal recorded as a skip.
fn evict(
    slot: &Path,
    path: &Path,
    bytes: u64,
    verb: &str,
    apply: bool,
    report: &mut Report,
) -> Option<u64> {
    if let Err(e) = remove_guarded(slot, path, apply) {
        report.skipped.push(format!("{} ({e})", path.display()));
        return None;
    }
    let credit = if apply {
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
        bytes
    } else {
        report.projected.project(path, bytes)
    };
    report.actions.push(format!(
        "{}{verb} {} ({bytes}B)",
        if apply { "" } else { "would " },
        path.display()
    ));
    Some(credit)
}

/// `slot` is the canonical, validated slot, held under its lease.
fn trim_slot_to(
    slot: &Path,
    target_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let Some(mut size) = retained(slot, report) else {
        return Ok(0);
    };
    if size <= target_bytes {
        return Ok(size);
    }
    let found = match shared_target::candidates(slot, SystemTime::now()) {
        Ok(found) => found,
        Err(e) => {
            note_incomplete(report, slot, &e);
            return Ok(size);
        }
    };
    for (candidate, bytes) in found {
        if size <= target_bytes {
            break;
        }
        if report.projected.covers(&candidate) {
            continue;
        }
        if let Some(credit) = evict(slot, &candidate, bytes, "pool_evict", apply, report) {
            size = size.saturating_sub(credit);
        }
    }
    if size <= target_bytes {
        return Ok(size);
    }
    // Remove idle slot profile subdirectories/files oldest-first when unit-level eviction is insufficient.
    let mut entries = vec![];
    match fs::read_dir(slot) {
        Ok(read_dir) => {
            for entry in read_dir {
                let path = match entry {
                    Ok(entry) => entry.path(),
                    Err(e) => {
                        note_incomplete(report, slot, &e);
                        continue;
                    }
                };
                if path.is_symlink() || report.projected.covers(&path) {
                    continue;
                }
                match scan(&path) {
                    Ok((bytes, modified)) => entries.push((modified, path, bytes)),
                    Err(e) => note_incomplete(report, &path, &e),
                }
            }
        }
        Err(e) => note_incomplete(report, slot, &e),
    }
    entries.sort_by_key(|(modified, _, _)| *modified);
    for (_, path, bytes) in entries {
        if size <= target_bytes {
            break;
        }
        if let Some(credit) = evict(slot, &path, bytes, "pool_trim", apply, report) {
            size = size.saturating_sub(credit);
        }
    }
    Ok(if apply {
        retained(slot, report).unwrap_or(size)
    } else {
        size
    })
}

/// Keep each idle slot under `slot_cap_bytes` and total across `slots` under `N * slot_cap_bytes`.
/// `primary` names the pool the slots must belong to.
pub fn enforce_pool_cap(
    primary: &Path,
    slots: &[PathBuf],
    slot_cap_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let total_cap = slot_cap_bytes.saturating_mul(slots.len() as u64);
    enforce_total(primary, slots, slot_cap_bytes, total_cap, apply, report)
}

/// Review slots are one per seat run, so their count says nothing about how much disk the pool
/// may use: each idle slot stays under `slot_cap_bytes`, and the slots together under a fixed
/// `DEFAULT_POOL_SLOTS * slot_cap_bytes`, however many abandoned ones are lying around. The
/// directories themselves are the launch owner's to remove.
pub fn enforce_review_slots(
    primary: &Path,
    slots: &[PathBuf],
    slot_cap_bytes: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let total_cap = slot_cap_bytes.saturating_mul(crate::cargo_pool::DEFAULT_POOL_SLOTS as u64);
    enforce_total(primary, slots, slot_cap_bytes, total_cap, apply, report)
}

fn enforce_total(
    primary: &Path,
    slots: &[PathBuf],
    slot_cap_bytes: u64,
    total_cap: u64,
    apply: bool,
    report: &mut Report,
) -> io::Result<u64> {
    let mut sizes = vec![0u64; slots.len()];
    let mut idle_flags = vec![false; slots.len()];
    let mut canonical = vec![PathBuf::new(); slots.len()];
    for (idx, slot) in slots.iter().enumerate() {
        match trusted_slot(primary, slot) {
            Ok(Some(path)) => canonical[idx] = path,
            Ok(None) => continue,
            Err(e) => {
                report
                    .skipped
                    .push(format!("{} (untrusted slot: {e})", slot.display()));
                continue;
            }
        }
        let Some(_lease) = try_exclusive_lease(slot)? else {
            sizes[idx] = retained(&canonical[idx], report).unwrap_or(0);
            if sizes[idx] > slot_cap_bytes {
                report.skipped.push(format!("{} (leased)", slot.display()));
            }
            continue;
        };
        idle_flags[idx] = true;
        sizes[idx] = trim_slot_to(&canonical[idx], slot_cap_bytes, apply, report)?;
    }

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
            let after = trim_slot_to(&canonical[idx], desired, apply, report)?;
            total = total.saturating_sub(sizes[idx].saturating_sub(after));
            sizes[idx] = after;
        }
    }
    Ok(total)
}

#[cfg(test)]
#[path = "cargo_pool_trim_tests.rs"]
mod tests;
