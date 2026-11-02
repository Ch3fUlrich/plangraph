//! Stale reclaim for the slot table: drop dead entries, free leftover seat
//! index locks (plan rev 6.4, R3b).
//!
//! [`reclaim`] is pure apart from the [`GroupLiveness`]/[`SeatLocks`] probes
//! and an injected `now_ms`; the caller runs it inside [`super::with_table`]
//! and touches `slots.wake` afterwards when anything was freed. Group
//! liveness is authoritative: a slot is stale only when its execution group is
//! empty or gone -- a dead supervisor pid, or a reused pid with another start
//! time, does NOT free a slot while the group still has members, and an
//! [`GroupState::Unknown`] group keeps the slot (fail safe) and lists it in
//! [`ReclaimReport::kept_unknown`]. Seat index locks get the same fail-safe
//! treatment: only a [`LockState::Free`] answer that is also older than
//! [`ReclaimPolicy::lock_grace_ms`] is deleted, a [`LockState::Unknown`] lock
//! is kept and listed in [`ReclaimReport::index_locks_unknown`].

use super::liveness::{GroupLiveness, GroupState, LockState, SeatLocks};
use super::{SlotClass, SlotTable};
use std::path::{Path, PathBuf};

/// How an unbound (pending) slot expires and how long it may sit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReclaimPolicy {
    /// TTL of a pending (unbound) entry in ms: 60 s (D4). The boundary itself
    /// frees; a pending entry whose creator is dead is reclaimed at once.
    pub pending_ttl_ms: u64,
    /// Minimum age of a `Free` seat index lock before it may be deleted:
    /// 10 s. Git closes its lock fd just before the rename, so a lock younger
    /// than this may still be in flight; it is kept and counted as held.
    pub lock_grace_ms: u64,
}

impl Default for ReclaimPolicy {
    fn default() -> Self {
        ReclaimPolicy {
            pending_ttl_ms: 60_000,
            lock_grace_ms: 10_000,
        }
    }
}

/// What [`reclaim`] changed, and what it refused to touch.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ReclaimReport {
    /// Active slots dropped from the table.
    pub freed: usize,
    /// Run ids of slots kept because their group state was
    /// [`GroupState::Unknown`].
    pub kept_unknown: Vec<String>,
    /// Queued tickets dropped from the table.
    pub tickets_dropped: usize,
    /// Leftover seat `index.lock` files deleted.
    pub index_locks_removed: usize,
    /// Seat `index.lock` files left in place because a live process holds
    /// them, or because they are younger than
    /// [`ReclaimPolicy::lock_grace_ms`] and may still be in flight.
    pub index_locks_held: usize,
    /// Seat `index.lock` files left in place because their state could not be
    /// determined ([`LockState::Unknown`], fail safe).
    pub index_locks_unknown: Vec<PathBuf>,
    /// Lock paths whose removal failed; reported here, never a panic.
    pub index_lock_errors: Vec<PathBuf>,
}

/// Drop every stale entry of `table` and report what happened.
///
/// Rules (plan rev 6.4, D4):
///
/// - a bound slot with a group is freed only when its group is
///   [`GroupState::Empty`] or [`GroupState::Gone`]; [`GroupState::Live`] keeps
///   it even when its pid is dead or reused, [`GroupState::Unknown`] keeps it
///   and lists it in [`ReclaimReport::kept_unknown`];
/// - a bound slot without a group is freed when its `(pid, start_time)`
///   process is no longer alive;
/// - an unbound (pending) slot is freed when `now_ms - created_ms >=
///   pending_ttl_ms` (the boundary itself frees), or at once when its creator
///   is not alive;
/// - a queued ticket is dropped when its waiter is not alive; the other
///   tickets keep their order;
/// - for every freed seat slot a leftover `.git/worktrees/seat-NN/index.lock`
///   is removed only when [`SeatLocks`] answers [`LockState::Free`] for it and
///   it is older than [`ReclaimPolicy::lock_grace_ms`]; a held or
///   still-young lock counts as held, a [`LockState::Unknown`] lock is kept
///   (fail safe) and listed in [`ReclaimReport::index_locks_unknown`]. The
///   lock states of one pass are probed with a single
///   [`SeatLocks::lock_states`] call before any file is touched.
pub fn reclaim(
    table: &mut SlotTable,
    live: &impl GroupLiveness,
    seats: &impl SeatLocks,
    now_ms: u64,
    policy: &ReclaimPolicy,
) -> ReclaimReport {
    let mut report = ReclaimReport::default();
    let mut kept = Vec::with_capacity(table.active.len());
    let mut lock_paths: Vec<PathBuf> = Vec::new();
    for slot in std::mem::take(&mut table.active) {
        let stale = if slot.bound {
            match &slot.group {
                Some(group) => match live.group_state(group) {
                    GroupState::Empty | GroupState::Gone => true,
                    GroupState::Live(_) => false,
                    GroupState::Unknown => {
                        report.kept_unknown.push(slot.run_id.clone());
                        false
                    }
                },
                None => !live.process_alive(slot.pid, slot.start_time),
            }
        } else {
            now_ms.saturating_sub(slot.created_ms) >= policy.pending_ttl_ms
                || !live.process_alive(slot.pid, slot.start_time)
        };
        if stale {
            report.freed += 1;
            if slot.class == SlotClass::Seat {
                if let Some(seat_slot) = slot.seat_slot {
                    if let Some(path) = seats.index_lock(&slot.repo_id, seat_slot) {
                        lock_paths.push(path);
                    }
                }
            }
        } else {
            kept.push(slot);
        }
    }
    table.active = kept;
    reclaim_index_locks(seats, lock_paths, now_ms, policy, &mut report);

    for ticket in std::mem::take(&mut table.queued) {
        if live.process_alive(ticket.pid, ticket.start_time) {
            table.queued.push(ticket);
        } else {
            report.tickets_dropped += 1;
        }
    }
    report
}

/// Remove the leftover `.git/worktrees/seat-NN/index.lock` files of freed
/// seat slots -- only those no live process holds ([`LockState::Free`]) and
/// none younger than [`ReclaimPolicy::lock_grace_ms`] (git closes its lock fd
/// just before the rename). [`LockState::Unknown`] locks are kept (fail safe)
/// and listed in [`ReclaimReport::index_locks_unknown`]; a removal error is
/// reported in [`ReclaimReport::index_lock_errors`], never a panic. All lock
/// states are probed with ONE [`SeatLocks::lock_states`] call per pass.
fn reclaim_index_locks(
    seats: &impl SeatLocks,
    paths: Vec<PathBuf>,
    now_ms: u64,
    policy: &ReclaimPolicy,
    report: &mut ReclaimReport,
) {
    if paths.is_empty() {
        return;
    }
    let states = seats.lock_states(&paths);
    for (path, state) in paths.into_iter().zip(states) {
        match state {
            LockState::Held => report.index_locks_held += 1,
            LockState::Unknown => report.index_locks_unknown.push(path),
            LockState::Free => match lock_age(&path, now_ms, policy.lock_grace_ms) {
                LockAge::Gone => {}
                LockAge::Young => report.index_locks_held += 1,
                LockAge::Aged => match std::fs::remove_file(&path) {
                    Ok(()) => report.index_locks_removed += 1,
                    Err(_) => report.index_lock_errors.push(path),
                },
            },
        }
    }
}

/// How old a `Free` lock file looks right now.
enum LockAge {
    /// No file there any more: nothing to remove, nothing to count.
    Gone,
    /// Younger than the grace period, or of unreadable age: keep it (git may
    /// still be about to rename over it) and count it as held.
    Young,
    /// Older than the grace period: safe to remove.
    Aged,
}

/// Age of a lock file against the injected `now_ms`; an unreadable mtime
/// fails safe to [`LockAge::Young`] and is never removed.
fn lock_age(path: &Path, now_ms: u64, lock_grace_ms: u64) -> LockAge {
    let meta = match std::fs::metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return LockAge::Gone,
        Err(_) => return LockAge::Young,
    };
    let modified = match meta.modified() {
        Ok(modified) => modified,
        Err(_) => return LockAge::Young,
    };
    let mtime_ms = match modified.duration_since(std::time::SystemTime::UNIX_EPOCH) {
        Ok(age) => age.as_millis() as u64,
        // Not expressible as a since-epoch age: unreadable, fail safe.
        Err(_) => return LockAge::Young,
    };
    if mtime_ms > now_ms.saturating_sub(lock_grace_ms) {
        LockAge::Young
    } else {
        LockAge::Aged
    }
}

#[cfg(test)]
mod seat_tests;
#[cfg(test)]
mod tests;
