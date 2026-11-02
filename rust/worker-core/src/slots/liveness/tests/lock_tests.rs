//! Seat `index.lock` probe tests on injectable tempdir `/proc` roots:
//! identity (`dev`/`ino`) matching, fail-safe `Unknown` answers and the
//! one-walk batch. [`std::os::unix`] stays inside this Linux-only module.

use super::super::{LockState, ProcSeatLocks, SeatLocks};
use std::os::unix::fs::symlink;
use std::path::PathBuf;

fn proc_and_lock(name: &str) -> (tempfile::TempDir, PathBuf, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    let lock = tmp.path().join(name);
    std::fs::write(&lock, b"x").unwrap();
    (tmp, proc_root, lock)
}

/// A `/proc/<pid>/fd` link to the lock file means a live process holds it.
#[test]
fn fd_scan_finds_held_lock() {
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&lock, proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Held);
}

/// Links to other files (and non-numeric proc entries) do not hold the lock.
#[test]
fn fd_scan_ignores_other_targets() {
    let (tmp, proc_root, lock) = proc_and_lock("index.lock");
    let other = tmp.path().join("other.lock");
    std::fs::write(&other, b"x").unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&other, proc_root.join("321/fd/3")).unwrap();
    std::fs::create_dir_all(proc_root.join("self/fd")).unwrap();
    symlink(&lock, proc_root.join("self/fd/4")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Free);
}

/// The fd target is the kernel-canonical path: a lock queried through a
/// symlinked ancestor is the same file and must read Held (path text never
/// matches here, only `dev`/`ino` does).
#[test]
fn fd_scan_held_via_symlinked_ancestor_is_held() {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    let real = tmp.path().join("real");
    std::fs::create_dir(&real).unwrap();
    let lock = real.join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    symlink(&real, tmp.path().join("link")).unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&lock, proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    let via_link = tmp.path().join("link").join("index.lock");
    assert_eq!(seats.lock_state(&via_link), LockState::Held);
}

/// A lock file that is already gone cannot be held.
#[test]
fn fd_scan_missing_lock_is_free() {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(
        seats.lock_state(&tmp.path().join("index.lock")),
        LockState::Free
    );
}

/// A dangling fd target (the fd or its target vanished) is skipped: NotFound
/// is never a reason to keep a lock around.
#[test]
fn fd_scan_dangling_fd_target_is_free() {
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(proc_root.join("gone.lock"), proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Free);
}

/// With no readable /proc root nothing can be ruled out: Unknown (fail
/// safe), so the lock survives instead of being deleted under a live run.
#[test]
fn fd_scan_without_proc_root_is_unknown() {
    let (tmp, _proc_root, lock) = proc_and_lock("index.lock");
    let missing = tmp.path().join("missing-proc");
    let seats = ProcSeatLocks::new(&missing, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Unknown);
}

/// A same-uid holder whose fd dir is unreadable may hold the lock: Unknown.
/// As root the chmod does not deny access: skip only that assertion then.
#[test]
fn fd_scan_same_uid_unreadable_fd_dir_is_unknown() {
    use std::os::unix::fs::PermissionsExt;
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    let fd_dir = proc_root.join("321/fd");
    std::fs::create_dir_all(&fd_dir).unwrap();
    std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o000)).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    if std::fs::read_dir(&fd_dir).is_err() {
        assert_eq!(seats.lock_state(&lock), LockState::Unknown);
    }
    // Restore the mode before the tempdir drops its contents.
    std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// A same-uid holder whose `fd` entry is not a directory fails with ENOTDIR
/// -- not NotFound -- so the answer is Unknown on every host, root included.
#[test]
fn fd_scan_same_uid_broken_fd_dir_is_unknown() {
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    std::fs::create_dir_all(proc_root.join("321")).unwrap();
    std::fs::write(proc_root.join("321/fd"), b"not a dir").unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Unknown);
}

/// A holder of ANOTHER uid whose fd dir errors cannot hold our lock: it is
/// skipped, or root-owned pid 1 would make every lock Unknown on the host.
/// Needs `chown`; without it the uid rule cannot be staged and is skipped.
#[test]
fn fd_scan_other_uid_broken_fd_dir_is_skipped() {
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    let pid_dir = proc_root.join("321");
    std::fs::create_dir_all(&pid_dir).unwrap();
    std::fs::write(pid_dir.join("fd"), b"not a dir").unwrap();
    if std::os::unix::fs::chown(&pid_dir, Some(65_534), None).is_err() {
        return;
    }
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Free);
}

/// A Held match anywhere wins over the Unknown of an unreadable sibling
/// holder.
#[test]
fn fd_scan_held_match_beats_unknown() {
    let (_tmp, proc_root, lock) = proc_and_lock("index.lock");
    std::fs::create_dir_all(proc_root.join("300")).unwrap();
    std::fs::write(proc_root.join("300/fd"), b"not a dir").unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&lock, proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(seats.lock_state(&lock), LockState::Held);
}

/// One `lock_states` call answers a whole reclaim pass, keeping path order.
#[test]
fn lock_states_answers_all_paths_in_order() {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    let held = tmp.path().join("held.lock");
    let free = tmp.path().join("free.lock");
    std::fs::write(&held, b"x").unwrap();
    std::fs::write(&free, b"x").unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&held, proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert_eq!(
        seats.lock_states(&[held, free]),
        vec![LockState::Held, LockState::Free]
    );
}
