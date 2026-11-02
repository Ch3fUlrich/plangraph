//! Linux probe tests on injectable tempdir roots (plus two real `/proc`
//! checks that need the host): stat parsing, cgroup states, fd scans.

use super::linux::read_start_time;
use super::*;
use std::os::unix::fs::symlink;
use std::path::Path;

/// `/proc/<pid>/stat` text whose comm contains spaces and `) (` so the
/// "parse after the last `)`" rule is exercised.
fn stat_text(pid: u32, comm: &str, start_time: u64) -> String {
    let mut rest = vec!["S".to_string()];
    for i in 1..19 {
        rest.push(i.to_string());
    }
    rest.push(start_time.to_string());
    format!("{pid} ({comm}) {}", rest.join(" "))
}

fn write_stat(proc_root: &Path, pid: u32, comm: &str, start_time: u64) {
    let dir = proc_root.join(pid.to_string());
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("stat"), stat_text(pid, comm, start_time)).unwrap();
}

/// A comm with spaces and `) (` never confuses the starttime parse.
#[test]
fn proc_stat_comm_with_parens_and_spaces_matches() {
    let tmp = tempfile::tempdir().unwrap();
    write_stat(tmp.path(), 4242, "weird ) ( comm", 987_654_321);
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert_eq!(read_start_time(tmp.path(), 4242), Some(987_654_321));
    assert!(live.process_alive(4242, 987_654_321));
}

/// A starttime mismatch means the pid was reused: not alive.
#[test]
fn proc_stat_start_time_mismatch_is_not_alive() {
    let tmp = tempfile::tempdir().unwrap();
    write_stat(tmp.path(), 4242, "weird ) ( comm", 987_654_321);
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert!(!live.process_alive(4242, 1));
}

/// The calling process itself is alive with its own starttime.
#[test]
fn own_process_is_alive() {
    let live = LinuxLiveness::host();
    let pid = std::process::id();
    let start = read_start_time(Path::new("/proc"), pid).expect("own /proc stat");
    assert!(live.process_alive(pid, start));
    assert!(!live.process_alive(pid, start.wrapping_add(1)));
}

/// A reaped child leaves `/proc` and is not alive.
#[test]
fn reaped_child_is_not_alive() {
    let live = LinuxLiveness::host();
    let mut child = std::process::Command::new("true").spawn().expect("spawn");
    let pid = child.id();
    let start = read_start_time(Path::new("/proc"), pid).expect("child stat");
    child.wait().expect("wait");
    assert!(!live.process_alive(pid, start));
}

/// A missing cgroup directory is Gone.
#[test]
fn cgroup_missing_dir_is_gone() {
    let tmp = tempfile::tempdir().unwrap();
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert_eq!(live.group_state("/gone.scope"), GroupState::Gone);
}

/// An empty `cgroup.procs` is Empty.
#[test]
fn cgroup_empty_procs_is_empty() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("empty.scope")).unwrap();
    std::fs::write(tmp.path().join("empty.scope/cgroup.procs"), b"").unwrap();
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert_eq!(live.group_state("/empty.scope"), GroupState::Empty);
}

/// A non-empty `cgroup.procs` is Live(n) with n the member count.
#[test]
fn cgroup_non_empty_procs_is_live() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("busy.scope")).unwrap();
    std::fs::write(
        tmp.path().join("busy.scope/cgroup.procs"),
        b"101\n\n102\n103\n",
    )
    .unwrap();
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert_eq!(live.group_state("/busy.scope"), GroupState::Live(3));
}

/// An unreadable `cgroup.procs` is Unknown (fail safe).
#[test]
fn cgroup_unreadable_procs_is_unknown() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(tmp.path().join("odd.scope/cgroup.procs")).unwrap();
    let live = LinuxLiveness::new(tmp.path(), tmp.path());
    assert_eq!(live.group_state("/odd.scope"), GroupState::Unknown);
}

/// `index_lock` is `.git/worktrees/seat-NN/index.lock` with a two-digit seat.
#[test]
fn index_lock_is_zero_padded_under_git_worktrees() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();
    let seats = ProcSeatLocks::new(tmp.path(), move |repo: &str| match repo {
        "repo-a" => Some(root.clone()),
        _ => None,
    });
    let expect = |n: u32, dir: &str| {
        assert_eq!(
            seats.index_lock("repo-a", n).unwrap(),
            tmp.path()
                .join(".git/worktrees")
                .join(dir)
                .join("index.lock")
        );
    };
    expect(7, "seat-07");
    expect(42, "seat-42");
}

/// An unknown repo has no lock path.
#[test]
fn index_lock_of_unknown_repo_is_none() {
    let seats = ProcSeatLocks::new("/proc", |_: &str| None);
    assert_eq!(seats.index_lock("repo-x", 7), None);
}

/// A `/proc/<pid>/fd` link to the lock path means a live process holds it.
#[test]
fn fd_scan_finds_held_lock() {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    let lock = tmp.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&lock, proc_root.join("321/fd/3")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert!(seats.held_by_live_process(&lock));
}

/// Links to other paths (and non-numeric proc entries) do not hold the lock.
#[test]
fn fd_scan_ignores_other_targets() {
    let tmp = tempfile::tempdir().unwrap();
    let proc_root = tmp.path().join("proc");
    let lock = tmp.path().join("index.lock");
    let other = tmp.path().join("other.lock");
    std::fs::write(&lock, b"x").unwrap();
    std::fs::write(&other, b"x").unwrap();
    std::fs::create_dir_all(proc_root.join("321/fd")).unwrap();
    symlink(&other, proc_root.join("321/fd/3")).unwrap();
    std::fs::create_dir_all(proc_root.join("self/fd")).unwrap();
    symlink(&lock, proc_root.join("self/fd/4")).unwrap();
    let seats = ProcSeatLocks::new(&proc_root, |_: &str| None);
    assert!(!seats.held_by_live_process(&lock));
}

/// With no proc root at all nothing is held (fail: leave for a later pass).
#[test]
fn fd_scan_without_proc_root_is_not_held() {
    let tmp = tempfile::tempdir().unwrap();
    let lock = tmp.path().join("index.lock");
    std::fs::write(&lock, b"x").unwrap();
    let seats = ProcSeatLocks::new(tmp.path().join("missing-proc"), |_: &str| None);
    assert!(!seats.held_by_live_process(&lock));
}
