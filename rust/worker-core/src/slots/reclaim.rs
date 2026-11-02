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
//! [`ReclaimReport::kept_unknown`].

use super::liveness::{GroupLiveness, GroupState, SeatLocks};
use super::{SlotClass, SlotTable};
use std::path::PathBuf;

/// How an unbound (pending) slot expires and how long it may sit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReclaimPolicy {
    /// TTL of a pending (unbound) entry in ms: 60 s (D4). The boundary itself
    /// frees; a pending entry whose creator is dead is reclaimed at once.
    pub pending_ttl_ms: u64,
}

impl Default for ReclaimPolicy {
    fn default() -> Self {
        ReclaimPolicy {
            pending_ttl_ms: 60_000,
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
    /// them.
    pub index_locks_held: usize,
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
///   is removed only when no live process holds it ([`SeatLocks`]).
pub fn reclaim(
    table: &mut SlotTable,
    live: &impl GroupLiveness,
    seats: &impl SeatLocks,
    now_ms: u64,
    policy: &ReclaimPolicy,
) -> ReclaimReport {
    let mut report = ReclaimReport::default();
    let mut kept = Vec::with_capacity(table.active.len());
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
                    reclaim_index_lock(seats, &slot.repo_id, seat_slot, &mut report);
                }
            }
        } else {
            kept.push(slot);
        }
    }
    table.active = kept;

    for ticket in std::mem::take(&mut table.queued) {
        if live.process_alive(ticket.pid, ticket.start_time) {
            table.queued.push(ticket);
        } else {
            report.tickets_dropped += 1;
        }
    }
    report
}

/// Remove the leftover `.git/worktrees/seat-NN/index.lock` of a freed seat
/// slot -- only when no live process holds it. A removal error is reported in
/// [`ReclaimReport::index_lock_errors`], never a panic.
fn reclaim_index_lock(
    seats: &impl SeatLocks,
    repo_id: &str,
    seat_slot: u32,
    report: &mut ReclaimReport,
) {
    let Some(path) = seats.index_lock(repo_id, seat_slot) else {
        return;
    };
    if !path.exists() {
        return;
    }
    if seats.held_by_live_process(&path) {
        report.index_locks_held += 1;
        return;
    }
    match std::fs::remove_file(&path) {
        Ok(()) => report.index_locks_removed += 1,
        Err(_) => report.index_lock_errors.push(path),
    }
}

#[cfg(test)]
mod seat_tests;
#[cfg(test)]
mod tests;
