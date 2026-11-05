//! Seat `index.lock` handling of `reclaim`: removed only when the probe says
//! [`LockState::Free`] and the file is older than the grace period, kept when
//! held, young or unknown (real files in a tempdir, fake [`SeatLocks`]).

use super::super::ActiveSlot;
use super::tests::{bound, run_at, table, FakeLiveness, FakeSeats};
use super::*;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after the epoch")
        .as_millis() as u64
}

/// Set the mtime of `path` to `age_ms` before now (`File::set_modified`).
fn age(path: &Path, age_ms: u64) {
    let file = std::fs::File::options()
        .write(true)
        .open(path)
        .expect("open to set mtime");
    let when = UNIX_EPOCH + Duration::from_millis(now_ms().saturating_sub(age_ms));
    file.set_modified(when).expect("set mtime");
}

/// Reclaim at the real wall clock: lock ages are real file mtimes.
fn run(table: &mut SlotTable, live: &FakeLiveness, seats: &FakeSeats) -> ReclaimReport {
    run_at(table, live, seats, now_ms(), &ReclaimPolicy::default())
}

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

/// A freed seat slot removes its leftover `index.lock` when it is not held
/// and older than `lock_grace_ms` (20 s > 10 s here).
#[test]
fn freed_seat_slot_removes_unheld_index_lock() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    age(&lock, 20_000);
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.freed, 1);
    assert_eq!(report.index_locks_removed, 1);
    assert_eq!(report.index_locks_held, 0);
    assert!(!lock.exists());
}

/// A `Free` lock younger than `lock_grace_ms` (5 s < 10 s) may still be in
/// flight: kept and counted as held, never removed.
#[test]
fn fresh_free_index_lock_is_kept_as_held() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    age(&lock, 5_000);
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_locks_held, 1);
    assert!(lock.exists());
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

/// An `Unknown` `index.lock` is kept (fail safe) and listed in
/// `index_locks_unknown`, never deleted and never folded into "held".
#[test]
fn unknown_index_lock_is_kept_and_listed() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    seats.unknown.insert(lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_locks_held, 0);
    assert_eq!(report.index_locks_unknown, vec![lock.clone()]);
    assert!(lock.exists());
}

/// A removal error is reported in `index_lock_errors`, never a panic. The
/// lock here is a non-empty directory, so `lock_grace_ms: 0` skips the age
/// rule and the removal itself fails.
#[test]
fn index_lock_removal_error_is_reported() {
    let dir = tempfile::tempdir().unwrap();
    let lock = dir.path().join("index.lock");
    // A non-empty directory can never be removed with `remove_file`.
    std::fs::create_dir(&lock).unwrap();
    std::fs::write(lock.join("member"), b"x").unwrap();
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let policy = ReclaimPolicy {
        lock_grace_ms: 0,
        ..Default::default()
    };
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run_at(&mut table, &gone_scope(), &seats, now_ms(), &policy);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_lock_errors, vec![lock]);
}

/// A lock whose age cannot be read is kept and counted as held, never
/// removed. Here `metadata` fails with ENOTDIR because a plain file sits
/// where the lock's parent directory should be.
#[cfg(unix)]
#[test]
fn index_lock_unreadable_age_is_kept_as_held() {
    let dir = tempfile::tempdir().unwrap();
    let blocker = dir.path().join("blocker");
    std::fs::write(&blocker, b"not a dir").unwrap();
    let lock = blocker.join("index.lock");
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock.clone());
    let mut table = table(vec![stale_seat(Some(7))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 0);
    assert_eq!(report.index_locks_held, 1);
    assert!(report.index_lock_errors.is_empty());
}

/// The lock states of one reclaim pass come from ONE `lock_states` call over
/// all freed seat locks, in table order.
#[test]
fn lock_states_is_one_call_per_pass() {
    let dir = tempfile::tempdir().unwrap();
    let lock_a = dir.path().join("a.lock");
    let lock_b = dir.path().join("b.lock");
    std::fs::write(&lock_a, b"x").unwrap();
    std::fs::write(&lock_b, b"x").unwrap();
    age(&lock_a, 20_000);
    age(&lock_b, 20_000);
    let mut seats = FakeSeats::default();
    seats.locks.insert(("repo-a".into(), 7), lock_a.clone());
    seats.locks.insert(("repo-a".into(), 8), lock_b.clone());
    let mut table = table(vec![stale_seat(Some(7)), stale_seat(Some(8))], Vec::new());
    let report = run(&mut table, &gone_scope(), &seats);
    assert_eq!(report.index_locks_removed, 2);
    assert_eq!(seats.batch_calls.get(), 1);
    assert_eq!(*seats.batch_paths.borrow(), vec![lock_a, lock_b]);
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
