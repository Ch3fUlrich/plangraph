//! Pull-based promotion out of the slot queue: backfill, bounded aging and
//! the exiting holder's release (plan rev 6.4, R3b-1). Pure policy plus small
//! table edits; stale reclaim follows in R3b-2.
//!
//! Every waiting supervisor runs [`promote`] on its OWN ticket under
//! `slots.lock` (after a [`crate::slots::touch_wake`] or a 1 s timeout) and
//! wins only when it is the oldest ticket that may run right now: eligible
//! (the class caps and the memory floor, the same tests as
//! [`crate::slots::decide`]) with no older ticket blocking it. An older
//! [`crate::slots::policy::first_blocker`] blocks only while it is eligible or
//! a [`is_reservation`]; every other older ticket is backfilled around, and
//! past [`AgingPolicy::release_after_ms`] a stuck ticket blocks nothing at
//! all. [`take_promotion`] turns the winner's ticket into an active row,
//! [`release`] drops an exiting holder's row (the caller then touches the wake
//! file, never while `slots.lock` is held), and [`aging_report`] lists the
//! run_ids R6/R8 alert on.

use super::policy::{
    class_counts, effective_free_mb, eligible, first_blocker, is_reservation, mem_fit, order_key,
    room_for,
};
use super::{ActiveSlot, SlotTable, Ticket};
use crate::hosts::Caps;

/// The aging ladder (R3b), in milliseconds: [`Default`] is alert after 15
/// minutes, reservation after 10, escalation after 20, release after 30.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AgingPolicy {
    /// Age that raises an alert (15 min).
    pub alert_after_ms: u64,
    /// Age from which a ticket waiting on memory alone stops younger tickets
    /// from backfilling around it (10 min).
    pub reserve_after_ms: u64,
    /// Age that escalates to routing (20 min).
    pub escalate_after_ms: u64,
    /// Age after which the ticket blocks nothing (30 min).
    pub release_after_ms: u64,
}

impl Default for AgingPolicy {
    fn default() -> Self {
        AgingPolicy {
            alert_after_ms: 900_000,
            reserve_after_ms: 600_000,
            escalate_after_ms: 1_200_000,
            release_after_ms: 1_800_000,
        }
    }
}

/// Why a promotion attempt waits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitReason {
    /// The ticket's class caps have no room.
    Caps,
    /// The ticket's reservation does not fit above the memory floor.
    Memory,
    /// An older ticket blocks the queue; carries the first blocker's run_id.
    Older(String),
}

/// What [`promote`] made of one queued ticket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Promotion {
    /// The ticket gets a slot (starting unbound).
    Promoted(ActiveSlot),
    /// The ticket stays queued, for this reason.
    Wait(WaitReason),
    /// No queued ticket carries this run_id.
    NotQueued,
}

/// May the caller promote ITSELF right now? (R3b, pure.)
///
/// [`Promotion::Promoted`] iff the ticket with `run_id` is queued, eligible
/// ([`crate::slots::policy::eligible`]) and no older ticket blocks it
/// ([`crate::slots::policy::first_blocker`]); the order is total,
/// `(enqueued_ms, run_id)` ascending. Otherwise [`Promotion::Wait`] carries
/// the reason — `Caps`/`Memory` when the ticket itself is not eligible, else
/// `Older` with the first blocking ticket's run_id — and
/// [`Promotion::NotQueued`] when no queued ticket matches at all.
///
/// A promoted slot has the same shape as [`crate::slots::decide`]'s grant:
/// `bound = false`, `created_ms = now_ms`, `seat_slot = None`, `group = None`
/// and the ticket's other fields. The caller applies it with
/// [`take_promotion`] inside [`crate::slots::with_table`].
pub fn promote(
    table: &SlotTable,
    run_id: &str,
    caps: &Caps,
    mem_available_mb: u64,
    rss_of: impl Fn(&ActiveSlot) -> u64,
    now_ms: u64,
    policy: &AgingPolicy,
) -> Promotion {
    let Some(ticket) = table.queued.iter().find(|t| t.run_id == run_id) else {
        return Promotion::NotQueued;
    };
    let counts = class_counts(&table.active);
    let free = effective_free_mb(mem_available_mb, &table.active, &rss_of);
    if !room_for(counts, ticket.class, caps) {
        return Promotion::Wait(WaitReason::Caps);
    }
    if !mem_fit(free, ticket.reservation_mb, caps) {
        return Promotion::Wait(WaitReason::Memory);
    }
    match first_blocker(&table.queued, ticket, counts, caps, free, now_ms, policy) {
        Some(blocker) => Promotion::Wait(WaitReason::Older(blocker.run_id.clone())),
        None => Promotion::Promoted(ActiveSlot {
            run_id: ticket.run_id.clone(),
            pid: ticket.pid,
            start_time: ticket.start_time,
            role: ticket.role,
            class: ticket.class,
            repo_id: ticket.repo_id.clone(),
            seat_slot: None,
            group: None,
            reservation_mb: ticket.reservation_mb,
            created_ms: now_ms,
            bound: false,
        }),
    }
}

/// Apply a [`Promotion::Promoted`] slot: drop the ticket with `slot.run_id`
/// from `queued` (the order of the other tickets is untouched) and append the
/// slot to `active`.
pub fn take_promotion(table: &mut SlotTable, slot: ActiveSlot) {
    if let Some(pos) = table.queued.iter().position(|t| t.run_id == slot.run_id) {
        table.queued.remove(pos);
    }
    table.active.push(slot);
}

/// The exiting holder deletes its own active row (R3b): only the row matching
/// `run_id`, `pid` AND `start_time` goes, so a pid reused by another attempt
/// never loses its slot. True when a row was removed. The caller touches
/// `<state>/slots.wake` AFTER [`crate::slots::with_table`] returns — never
/// while `slots.lock` is held.
pub fn release(table: &mut SlotTable, run_id: &str, pid: u32, start_time: u64) -> bool {
    let hit = table
        .active
        .iter()
        .position(|s| s.run_id == run_id && s.pid == pid && s.start_time == start_time);
    match hit {
        Some(pos) => {
            table.active.remove(pos);
            true
        }
        None => false,
    }
}

/// What [`aging_report`] saw in the queue right now; run_id lists in queue
/// order ([`(enqueued_ms, run_id)`](super::policy::order_key)). R6/R8 send the
/// alerts.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AgingReport {
    /// Aged past [`AgingPolicy::alert_after_ms`].
    pub alert: Vec<String>,
    /// Currently a reservation: aged into the reservation window and waiting
    /// on memory alone.
    pub reserved: Vec<String>,
    /// Aged past [`AgingPolicy::escalate_after_ms`] and not eligible.
    pub escalate: Vec<String>,
    /// Aged past [`AgingPolicy::release_after_ms`]: blocks nothing any more.
    pub released: Vec<String>,
}

/// The four aging lists over the queued tickets (R3b, pure): `now_ms` is the
/// injected clock, `policy` the ladder. A ticket can appear in several lists
/// at once — escalation keeps firing until the ticket is released.
pub fn aging_report(
    table: &SlotTable,
    now_ms: u64,
    policy: &AgingPolicy,
    caps: &Caps,
    mem_available_mb: u64,
    rss_of: impl Fn(&ActiveSlot) -> u64,
) -> AgingReport {
    let counts = class_counts(&table.active);
    let free = effective_free_mb(mem_available_mb, &table.active, &rss_of);
    let mut queued: Vec<&Ticket> = table.queued.iter().collect();
    queued.sort_by(|a, b| order_key(a).cmp(&order_key(b)));
    let mut report = AgingReport::default();
    for ticket in queued {
        let age = now_ms.saturating_sub(ticket.enqueued_ms);
        if age >= policy.alert_after_ms {
            report.alert.push(ticket.run_id.clone());
        }
        if is_reservation(counts, ticket, caps, now_ms, policy) {
            report.reserved.push(ticket.run_id.clone());
        }
        if age >= policy.escalate_after_ms && !eligible(counts, ticket, caps, free) {
            report.escalate.push(ticket.run_id.clone());
        }
        if age >= policy.release_after_ms {
            report.released.push(ticket.run_id.clone());
        }
    }
    report
}
