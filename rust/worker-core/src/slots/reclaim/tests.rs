//! Reclaim rule tests: fake [`GroupLiveness`]/[`SeatLocks`] probes, injected
//! `now_ms`, one test per rule of `reclaim`.

use super::super::{ActiveSlot, Ticket};
use super::*;
use crate::hosts::Role;
use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

const NOW: u64 = 1_000_000;

#[derive(Default)]
pub(super) struct FakeLiveness {
    pub(super) groups: HashMap<String, GroupState>,
    pub(super) alive: HashSet<(u32, u64)>,
}

impl GroupLiveness for FakeLiveness {
    fn group_state(&self, group: &str) -> GroupState {
        self.groups
            .get(group)
            .copied()
            .unwrap_or(GroupState::Unknown)
    }

    fn process_alive(&self, pid: u32, start_time: u64) -> bool {
        self.alive.contains(&(pid, start_time))
    }
}

#[derive(Default)]
pub(super) struct FakeSeats {
    pub(super) locks: HashMap<(String, u32), PathBuf>,
    pub(super) held: HashSet<PathBuf>,
    pub(super) unknown: HashSet<PathBuf>,
    pub(super) calls: Cell<u32>,
    pub(super) batch_calls: Cell<u32>,
    pub(super) batch_paths: RefCell<Vec<PathBuf>>,
}

impl FakeSeats {
    fn state_of(&self, path: &Path) -> LockState {
        if self.held.contains(path) {
            LockState::Held
        } else if self.unknown.contains(path) {
            LockState::Unknown
        } else {
            LockState::Free
        }
    }
}

impl SeatLocks for FakeSeats {
    fn index_lock(&self, repo_id: &str, seat_slot: u32) -> Option<PathBuf> {
        self.calls.set(self.calls.get() + 1);
        self.locks.get(&(repo_id.to_string(), seat_slot)).cloned()
    }

    fn lock_state(&self, path: &Path) -> LockState {
        self.state_of(path)
    }

    fn lock_states(&self, paths: &[PathBuf]) -> Vec<LockState> {
        self.batch_calls.set(self.batch_calls.get() + 1);
        self.batch_paths.borrow_mut().extend(paths.iter().cloned());
        paths.iter().map(|path| self.state_of(path)).collect()
    }
}

pub(super) fn bound(run_id: &str) -> ActiveSlot {
    ActiveSlot {
        run_id: run_id.into(),
        pid: 100,
        start_time: 1_000,
        role: Role::Implement,
        class: SlotClass::Writer,
        repo_id: "repo-a".into(),
        seat_slot: None,
        group: None,
        reservation_mb: 64,
        created_ms: 1_000,
        bound: true,
    }
}

pub(super) fn pending(run_id: &str, created_ms: u64) -> ActiveSlot {
    ActiveSlot {
        bound: false,
        created_ms,
        ..bound(run_id)
    }
}

pub(super) fn ticket(run_id: &str, pid: u32, start_time: u64, enqueued_ms: u64) -> Ticket {
    Ticket {
        run_id: run_id.into(),
        pid,
        start_time,
        enqueued_ms,
        role: Role::Review,
        class: SlotClass::Other,
        repo_id: "repo-b".into(),
        reservation_mb: 64,
    }
}

pub(super) fn table(active: Vec<ActiveSlot>, queued: Vec<Ticket>) -> SlotTable {
    SlotTable {
        version: 1,
        active,
        queued,
    }
}

/// Reclaim with the default policy (60 s pending TTL) at the injected `NOW`.
pub(super) fn run(table: &mut SlotTable, live: &FakeLiveness, seats: &FakeSeats) -> ReclaimReport {
    reclaim(table, live, seats, NOW, &ReclaimPolicy::default())
}

/// Reclaim with a custom policy at the injected `NOW`.
pub(super) fn run_policy(
    table: &mut SlotTable,
    live: &FakeLiveness,
    seats: &FakeSeats,
    policy: &ReclaimPolicy,
) -> ReclaimReport {
    reclaim(table, live, seats, NOW, policy)
}

/// Reclaim at a caller-chosen `now_ms` (seat lock ages are real file mtimes,
/// so those tests derive `now_ms` from `SystemTime`).
pub(super) fn run_at(
    table: &mut SlotTable,
    live: &FakeLiveness,
    seats: &FakeSeats,
    now_ms: u64,
    policy: &ReclaimPolicy,
) -> ReclaimReport {
    reclaim(table, live, seats, now_ms, policy)
}

/// Bound slot with a group: Empty frees (D4 group liveness rule).
#[test]
fn bound_slot_with_empty_group_is_freed() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Empty);
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 1);
    assert!(table.active.is_empty());
}

/// Bound slot with a group: Gone frees.
#[test]
fn bound_slot_with_gone_group_is_freed() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Gone);
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 1);
    assert!(table.active.is_empty());
}

/// A dead supervisor pid does NOT free a slot while the group has members.
#[test]
fn dead_supervisor_with_live_group_keeps_slot() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Live(3));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 0);
    assert_eq!(table.active.len(), 1);
}

/// A reused pid (same pid, another start time) does NOT free a live slot.
#[test]
fn reused_pid_with_live_group_keeps_slot() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    slot.start_time = 2_000;
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Live(1));
    live.alive.insert((slot.pid, 3_000));
    assert!(!live.process_alive(slot.pid, slot.start_time));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 0);
    assert_eq!(table.active.len(), 1);
}

/// Unknown keeps the slot (fail safe) and lists it in `kept_unknown`.
#[test]
fn unknown_group_keeps_and_lists_slot() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Unknown);
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 0);
    assert_eq!(report.kept_unknown, vec!["run-a".to_string()]);
    assert_eq!(table.active.len(), 1);
}

/// Bound slot without a group: freed when its process is not alive.
#[test]
fn bound_slot_without_group_is_freed_when_process_dead() {
    let slot = bound("run-a");
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &FakeLiveness::default(), &FakeSeats::default());
    assert_eq!(report.freed, 1);
    assert!(table.active.is_empty());
}

/// Bound slot without a group: kept when its exact (pid, start_time) is alive.
#[test]
fn bound_slot_without_group_kept_when_process_alive() {
    let slot = bound("run-a");
    let mut live = FakeLiveness::default();
    live.alive.insert((slot.pid, slot.start_time));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 0);
    assert_eq!(table.active.len(), 1);
}

/// Bound slot without a group: a reused pid is not its process, so it frees.
#[test]
fn bound_slot_without_group_freed_when_pid_reused() {
    let slot = bound("run-a");
    let mut live = FakeLiveness::default();
    live.alive.insert((slot.pid, slot.start_time + 1));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 1);
}

/// Pending slot just inside the 60 s TTL is kept (creator alive).
#[test]
fn pending_slot_kept_just_inside_ttl() {
    let slot = pending("run-a", NOW - 59_999);
    let mut live = FakeLiveness::default();
    live.alive.insert((slot.pid, slot.start_time));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 0);
    assert_eq!(table.active.len(), 1);
}

/// The TTL boundary itself frees: 60 s exactly, creator still alive.
#[test]
fn pending_slot_freed_at_ttl_boundary() {
    let slot = pending("run-a", NOW - 60_000);
    let mut live = FakeLiveness::default();
    live.alive.insert((slot.pid, slot.start_time));
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.freed, 1);
    assert!(table.active.is_empty());
}

/// A pending entry whose creator is dead is reclaimed at once (age 0 ms).
#[test]
fn pending_slot_freed_at_once_when_creator_dead() {
    let slot = pending("run-a", NOW);
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &FakeLiveness::default(), &FakeSeats::default());
    assert_eq!(report.freed, 1);
    assert!(table.active.is_empty());
}

/// A stale ticket (dead waiter) is dropped; the others keep their order.
#[test]
fn dead_ticket_dropped_others_keep_order() {
    let t1 = ticket("run-1", 10, 1_100, 10);
    let t2 = ticket("run-2", 20, 2_100, 20);
    let t3 = ticket("run-3", 30, 3_100, 30);
    let mut live = FakeLiveness::default();
    live.alive.insert((10, 1_100));
    live.alive.insert((30, 3_100));
    let mut table = table(Vec::new(), vec![t1.clone(), t2, t3.clone()]);
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.tickets_dropped, 1);
    assert_eq!(table.queued, vec![t1, t3]);
}

/// A ticket waiter behind a reused pid is gone: the ticket is dropped.
#[test]
fn ticket_with_reused_waiter_dropped() {
    let t1 = ticket("run-1", 10, 1_100, 10);
    let mut live = FakeLiveness::default();
    live.alive.insert((10, 2_200));
    let mut table = table(Vec::new(), vec![t1]);
    let report = run(&mut table, &live, &FakeSeats::default());
    assert_eq!(report.tickets_dropped, 1);
    assert!(table.queued.is_empty());
}

/// The injected `pending_ttl_ms` is the TTL that frees -- 5 s here, not the
/// hardcoded 60 s default. The boundary itself frees.
#[test]
fn pending_ttl_is_the_injected_policy_value() {
    let policy = ReclaimPolicy {
        pending_ttl_ms: 5_000,
        ..Default::default()
    };
    let kept = pending("run-a", NOW - 4_999);
    let freed = pending("run-b", NOW - 5_000);
    let mut live = FakeLiveness::default();
    live.alive.insert((kept.pid, kept.start_time));
    live.alive.insert((freed.pid, freed.start_time));
    let mut table = table(vec![kept, freed], Vec::new());
    let report = run_policy(&mut table, &live, &FakeSeats::default(), &policy);
    assert_eq!(report.freed, 1);
    assert_eq!(table.active.len(), 1);
    assert_eq!(table.active[0].run_id, "run-a");
}
