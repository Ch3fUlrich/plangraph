//! Pure admission policy for the slot table: no IO and no clock.
//!
//! [`effective_free_mb`] implements the accounting formula: only the part of a
//! reservation the run has not materialized yet counts against free memory.
//! [`decide`] then turns one request into a grant or a queue entry against the
//! R1a [`Caps`] of the host: class caps first, then the memory floor, then the
//! queue. Since R3b both [`decide`] and [`super::promote`] share one blocking
//! rule ([`first_blocker`]) and one aging ladder (the caller's
//! [`AgingPolicy`]): an older ticket blocks only while it is [`eligible`] or
//! a [`is_reservation`], everything else is backfilled around.

use super::promote::AgingPolicy;
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
pub(super) fn room_for(counts: ClassCounts, class: SlotClass, caps: &Caps) -> bool {
    match class {
        SlotClass::Writer => counts.writers < caps.max_writers && counts.runs < caps.max_runs,
        SlotClass::Seat => counts.seats < caps.max_seats && counts.runs < caps.max_runs,
        SlotClass::Other => counts.runs < caps.max_runs,
        SlotClass::Session => counts.sessions < caps.max_sessions,
    }
}

/// The queue's total order (R3b): `(enqueued_ms, run_id)` ascending, so two
/// tickets are never the same age and the order is deterministic.
pub(super) fn order_key(ticket: &Ticket) -> (u64, &str) {
    (ticket.enqueued_ms, ticket.run_id.as_str())
}

/// The memory half of the admission test (the R3a fix): `checked_sub`, so a
/// reservation above the free memory never passes, even with a zero floor.
pub(super) fn mem_fit(free_mb: u64, reservation_mb: u64, caps: &Caps) -> bool {
    free_mb
        .checked_sub(reservation_mb)
        .is_some_and(|left| left >= caps.mem_floor_mb)
}

/// Is `ticket` eligible right now (R3b): its class caps have room and its
/// reservation fits above the memory floor?
pub(super) fn eligible(counts: ClassCounts, ticket: &Ticket, caps: &Caps, free_mb: u64) -> bool {
    room_for(counts, ticket.class, caps) && mem_fit(free_mb, ticket.reservation_mb, caps)
}

/// Is `ticket` a reservation (R3b): aged into
/// `[reserve_after_ms, release_after_ms)` and waiting on memory alone (its
/// class caps have room)? A ticket that waits only on its class cap never
/// becomes a reservation.
pub(super) fn is_reservation(
    counts: ClassCounts,
    ticket: &Ticket,
    caps: &Caps,
    now_ms: u64,
    policy: &AgingPolicy,
) -> bool {
    let age = now_ms.saturating_sub(ticket.enqueued_ms);
    policy.reserve_after_ms <= age
        && age < policy.release_after_ms
        && room_for(counts, ticket.class, caps)
}

/// The one blocking rule (R3b), shared by [`decide`] and
/// [`super::promote`]: the first queued ticket strictly older than `request`
/// in [`order_key`] order that is [`eligible`] or a [`is_reservation`] blocks
/// it. Every other older ticket is backfilled around, and the request's own
/// queued row never blocks itself (its key is not strictly earlier).
pub(super) fn first_blocker<'a>(
    queued: &'a [Ticket],
    request: &Ticket,
    counts: ClassCounts,
    caps: &Caps,
    free_mb: u64,
    now_ms: u64,
    policy: &AgingPolicy,
) -> Option<&'a Ticket> {
    let mut first: Option<&'a Ticket> = None;
    for ticket in queued {
        if order_key(ticket) >= order_key(request) {
            continue;
        }
        if !eligible(counts, ticket, caps, free_mb)
            && !is_reservation(counts, ticket, caps, now_ms, policy)
        {
            continue;
        }
        match first {
            Some(older) if order_key(older) <= order_key(ticket) => {}
            _ => first = Some(ticket),
        }
    }
    first
}

/// Admit one request: grant a slot (starting `bound = false`) or hand the
/// ticket back to be appended to the queue.
///
/// A grant needs all three of:
///
/// 1. class caps have room for `request.class` ([`room_for`]),
/// 2. `effective_free_mb - request.reservation_mb >= caps.mem_floor_mb`
///    ([`mem_fit`]),
/// 3. no older queued ticket blocks it ([`first_blocker`]): an older ticket
///    blocks only while it is eligible or a reservation (aged past
///    [`AgingPolicy::reserve_after_ms`], waiting on memory alone), so a new
///    arrival backfills around the rest. `policy` is the same
///    [`AgingPolicy`] ladder [`super::promote`] takes — one ladder and one
///    blocking rule for both halves of the queue; `now_ms` ages the queue.
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
    policy: &AgingPolicy,
) -> Decision {
    let counts = class_counts(&table.active);
    let free = effective_free_mb(mem_available_mb, &table.active, &rss_of);
    let caps_ok = room_for(counts, request.class, caps);
    let mem_ok = mem_fit(free, request.reservation_mb, caps);
    let order_ok =
        first_blocker(&table.queued, request, counts, caps, free, now_ms, policy).is_none();
    if caps_ok && mem_ok && order_ok {
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
