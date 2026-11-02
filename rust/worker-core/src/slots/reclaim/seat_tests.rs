//! Seat `index.lock` handling of `reclaim`: removed when no live process
//! holds it, kept when one does, removal errors reported (real files in a
//! tempdir, fake [`SeatLocks`]).

use super::super::ActiveSlot;
use super::tests::{bound, run, table, FakeLiveness, FakeSeats};
use super::*;

/// A seat slot that is stale (its group is Gone) and must be freed.
fn stale_seat(seat_slot: Option<u32>) -> ActiveSlot {
    let mut slot = bound("run-a");
    slot.class = SlotClass::Seat;
    slot.seat_slot = seat_slot;
    slot.group = Some("/scope-a".into());
    slot
}

fn gone_scope() -> FakeLiveness {
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Gone);
    live
}

/// A freed seat slot removes its leftover `index.lock` when it is not held.
#[test]
fn freed_seat_slot_removes_unheld_index_lock() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.freed, 1);
    assert_eq!(report.index_locks_removed, 1);
    assert_eq!(report.index_locks_held, 0);
    assert!(!lock.exists());
}

/// A held `index.lock` is left in place and counted as held.
#[test]
fn freed_seat_slot_keeps_held_index_lock() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    seats.held.insert(lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_locks_held, 1);
    assert!(lock.exists());
}

/// A removal error is reported in `index_lock_errors`, never a panic.
#[test]
fn index_lock_removal_error_is_reported() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    // A non-empty directory can never be removed with `remove_file`.
    std::fs::create_dir(&lock).unwrap();
    std::fs::write(lock.join("member"), b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_lock_errors, vec![lock]);
}

/// A lock path the repo map does not know is not counted at all.
#[test]
fn missing_index_lock_is_ignored() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock"); // never created
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock);
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.freed, 1);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_locks_held, 0);
    assert!(report.index_lock_errors.is_empty());
}

/// Only freed seat slots touch index locks; other classes never do.
#[test]
fn non_seat_freed_slot_never_touches_locks() {
    let mut slot = bound("run-a");
    slot.group = Some("/scope-a".into());
    let seats = FakeSeats::default();
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.freed, 1);
    assert_eq!(seats.calls.get(), 0);
    assert_eq!(report.index_locks_removed, 0);
}

/// A freed seat slot without a number has no `seat-NN` lock to reclaim.
#[test]
fn freed_seat_slot_without_number_never_touches_locks() {
    let seats = FakeSeats::default();
    let mut table = table(vec![stale_seat(None)], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.freed, 1);
    assert_eq!(seats.calls.get(), 0);
    assert_eq!(report.index_locks_removed, 0);
}

/// Kept slots never touch their seat locks.
#[test]
fn kept_seat_slot_never_touches_locks() {
    let mut slot = stale_seat(Some(7));
    slot.group = Some("/scope-a".into());
    let mut live = FakeLiveness::default();
    live.groups.insert("/scope-a".into(), GroupState::Live(1));
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![slot], Vec::new());
    let report = run(&mut table, &live, &seats);
    assert_eq!(report.freed, 0);
    assert_eq!(report.index_locks_removed, 0);
    assert!(lock.exists());
}
