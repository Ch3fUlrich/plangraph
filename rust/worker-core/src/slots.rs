//! Per-host slot table: which runs hold a slot and which wait in line
//! (plan rev 6.4, R3a).
//!
//! `<state>/slots.json` is never locked itself; it is read and rewritten only
//! while `<state>/slots.lock` is held (see [`crate::lockfile`]), by an atomic
//! replace. [`with_table`] is the only entry point callers need: it loads the
//! table (a missing file is an empty table), runs a closure over it and saves
//! only when the closure changed something.
//!
//! The policy half is pure: [`effective_free_mb`] subtracts only the
//! not-yet-materialized part of every reservation, and [`decide`] grants or
//! queues one request against the R1a [`crate::hosts::Caps`] of the host
//! (queue order `(enqueued_ms, run_id)` across classes; R3b adds promotion,
//! aging and reclaim).
//!
//! Roster shape:
//!
//! - [`SlotClass`] picks the caps a slot counts against.
//! - [`ActiveSlot`] is one granted slot; `bound = false` until the run is
//!   materialized (group/seat are known and the reservation starts to show up
//!   as RSS).
//! - [`Ticket`] is one request in the queue, stamped on arrival.
//! - [`SlotTable`] is the whole file: `version`, `active`, `queued`.
//!
//! R3b-1 turns the queue pull-based: each waiting supervisor runs [`promote`]
//! on its own ticket and takes it only when it is the oldest that fits
//! ([`take_promotion`]); the exiting holder calls [`release`] and touches
//! [`touch_wake`] outside the lock. R3b-2 adds stale [`reclaim`] by execution
//! group liveness.

use crate::hosts::Role;
use serde::{Deserialize, Serialize};

mod io;
mod liveness;
mod policy;
mod promote;
#[cfg(test)]
mod promote_tests;
mod reclaim;
#[cfg(test)]
mod tests;
mod wake;
#[cfg(test)]
mod wake_tests;

pub use io::{with_table, SlotError};
#[cfg(windows)]
pub use liveness::{FileSeatLocks, JobLiveness};
pub use liveness::{GroupLiveness, GroupState, LockState, SeatLocks};
#[cfg(target_os = "linux")]
pub use liveness::{LinuxLiveness, ProcSeatLocks};
pub use policy::{decide, effective_free_mb, Decision};
pub use promote::{
    aging_report, promote, release, take_promotion, AgingPolicy, AgingReport, Promotion, WaitReason,
};
pub use reclaim::{reclaim, ReclaimPolicy, ReclaimReport};
pub use wake::{touch_wake, wait_for_wake, wake_token, WakeResult};

/// Which caps a slot counts against (V6).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SlotClass {
    /// Counts against `max_writers` and `max_runs`.
    Writer,
    /// Counts against `max_seats` and `max_runs`.
    Seat,
    /// Counts against `max_runs` only.
    Other,
    /// Counts against `max_sessions` only.
    Session,
}

/// One granted slot in the host's slot table.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub struct ActiveSlot {
    /// Attempt this slot belongs to.
    pub run_id: String,
    pub pid: u32,
    /// Opaque process start time (Linux: starttime ticks, Windows: creation
    /// FILETIME); with `pid` it guards against pid reuse (D-3705).
    pub start_time: u64,
    /// Work the run does; taken from the R1a role vocabulary.
    pub role: Role,
    pub class: SlotClass,
    pub repo_id: String,
    /// Number of the interactive seat for `SlotClass::Seat` runs.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seat_slot: Option<u32>,
    /// Confinement group: cgroup scope (Linux) or Job Object name (Windows).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub group: Option<String>,
    /// Memory this slot promises to keep free for the run, in MB.
    pub reservation_mb: u64,
    /// When the slot was granted (epoch ms).
    pub created_ms: u64,
    /// False until the run is materialized and its RSS starts to show up.
    pub bound: bool,
}

/// One request in the queue, or on its way in.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub struct Ticket {
    /// Attempt this request belongs to.
    pub run_id: String,
    pub pid: u32,
    /// Opaque process start time (Linux: starttime ticks, Windows: creation
    /// FILETIME); with `pid` it guards against pid reuse (D-3705).
    pub start_time: u64,
    /// Arrival stamp (epoch ms): the FIFO order of the queue.
    pub enqueued_ms: u64,
    /// Work the run does; taken from the R1a role vocabulary.
    pub role: Role,
    pub class: SlotClass,
    pub repo_id: String,
    /// Memory the run asks to reserve, in MB.
    pub reservation_mb: u64,
}

/// The whole `<state>/slots.json` file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub struct SlotTable {
    /// File format version; only 1 exists.
    pub version: u32,
    /// Granted slots, one per running attempt.
    pub active: Vec<ActiveSlot>,
    /// Waiting requests, FIFO by `enqueued_ms`.
    pub queued: Vec<Ticket>,
}

impl Default for SlotTable {
    fn default() -> Self {
        SlotTable {
            version: 1,
            active: Vec::new(),
            queued: Vec::new(),
        }
    }
}
