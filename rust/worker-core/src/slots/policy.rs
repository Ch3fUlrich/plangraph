//! Pure admission policy for the slot table: no IO and no clock.
//!
//! [`effective_free_mb`] implements the accounting formula: only the part of a
//! reservation the run has not materialized yet counts against free memory.
//! [`decide`] then turns one request into a grant or a queue entry against the
//! R1a [`Caps`] of the host: class caps first, then the memory floor, then FIFO
//! order (`enqueued_ms`, across classes). R3b adds backfill and aging on top.

use super::{ActiveSlot, SlotClass, SlotTable, Ticket};
use crate::hosts::Caps;

/// What [`decide`] made of one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// The request gets a slot (starting unbound).
    Grant(ActiveSlot),
    /// The request waits; the caller appends this ticket to `SlotTable::queued`.
    Queue(Ticket),
}

/// Memory still free once every active reservation is honored:
/// `mem_available_mb - sum over active of max(0, reservation_mb - rss_of(slot))`,
/// saturating at 0. Only the not-yet-materialized part of a reservation is
/// subtracted: memory a run already holds as RSS is not counted twice.
pub fn effective_free_mb(
    mem_available_mb: u64,
    active: &[ActiveSlot],
    rss_of: impl Fn(&ActiveSlot) -> u64,
) -> u64 {
    let promised = active
        .iter()
        .map(|slot| slot.reservation_mb.saturating_sub(rss_of(slot)))
        .fold(0u64, u64::saturating_add);
    mem_available_mb.saturating_sub(promised)
}

/// How many active slots sit on each cap counter. `runs` counts `writer`,
/// `seat` and `other`; `session` slots sit on their own counter.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ClassCounts {
    pub runs: u64,
    pub writers: u64,
    pub seats: u64,
    pub sessions: u64,
}

/// Count the active slots against every cap counter.
pub fn class_counts(active: &[ActiveSlot]) -> ClassCounts {
    let mut counts = ClassCounts::default();
    for slot in active {
        match slot.class {
            SlotClass::Writer => {
                counts.writers += 1;
                counts.runs += 1;
            }
            SlotClass::Seat => {
                counts.seats += 1;
                counts.runs += 1;
            }
            SlotClass::Other => counts.runs += 1,
            SlotClass::Session => counts.sessions += 1,
        }
    }
    counts
}

/// Would one more slot of `class` fit under the caps? (V6 counters.)
fn room_for(counts: ClassCounts, class: SlotClass, caps: &Caps) -> bool {
    match class {
        SlotClass::Writer => counts.writers < caps.max_writers && counts.runs < caps.max_runs,
        SlotClass::Seat => counts.seats < caps.max_seats && counts.runs < caps.max_runs,
        SlotClass::Other => counts.runs < caps.max_runs,
        SlotClass::Session => counts.sessions < caps.max_sessions,
    }
}

/// Admit one request: grant a slot (starting `bound = false`) or hand the
/// ticket back to be appended to the queue.
///
/// A grant needs all three of:
///
/// 1. class caps have room for `request.class` ([`room_for`]),
/// 2. `effective_free_mb - request.reservation_mb >= caps.mem_floor_mb`,
/// 3. no queued ticket was enqueued strictly earlier than the request
///    (FIFO by `enqueued_ms` across classes; a queued ticket re-deciding
///    itself never blocks on its own stamp).
///
/// The caller applies the returned decision inside [`super::with_table`];
/// `now_ms` stamps `created_ms` of a granted slot. The request's `enqueued_ms`
/// is its arrival stamp and is kept on a queued ticket unchanged.
pub fn decide(
    table: &SlotTable,
    request: &Ticket,
    caps: &Caps,
    mem_available_mb: u64,
    rss_of: impl Fn(&ActiveSlot) -> u64,
    now_ms: u64,
) -> Decision {
    let caps_ok = room_for(class_counts(&table.active), request.class, caps);
    let free = effective_free_mb(mem_available_mb, &table.active, rss_of);
    // checked_sub: a reservation above the free memory never passes, even
    // with a zero floor (saturating to 0 would read as 0 >= 0).
    let mem_ok = free
        .checked_sub(request.reservation_mb)
        .is_some_and(|left| left >= caps.mem_floor_mb);
    let fifo_ok = !table
        .queued
        .iter()
        .any(|t| t.enqueued_ms < request.enqueued_ms);
    if caps_ok && mem_ok && fifo_ok {
        Decision::Grant(ActiveSlot {
            run_id: request.run_id.clone(),
            pid: request.pid,
            start_time: request.start_time,
            role: request.role,
            class: request.class,
            repo_id: request.repo_id.clone(),
            seat_slot: None,
            group: None,
            reservation_mb: request.reservation_mb,
            created_ms: now_ms,
            bound: false,
        })
    } else {
        Decision::Queue(request.clone())
    }
}
