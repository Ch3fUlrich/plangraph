//! Windows supervisor tests. Every test here is `#[serial]` (serial_test).
//! The handle-leak check samples the process-wide handle count in a child
//! that runs only the helper, because the other tests of this binary run in
//! parallel and move that count both ways.

use super::*;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::STILL_ACTIVE;
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetExitCodeProcess, GetProcessHandleCount, OpenProcess,
    PROCESS_QUERY_LIMITED_INFORMATION,
};

use crate::stall::{supervise, supervise_with_cancel, Opts};
use serial_test::serial;

/// Set only for helper runs; a plain `cargo test` sees it unset and skips.
const HELPER_ENV: &str = "PG_STALL_TEST_HELPER";
/// Directory for the helper's pid files (a fresh temp dir per test).
const HELPER_DIR: &str = "PG_STALL_TEST_HELPER_DIR";

fn quick() -> Opts {
    Opts {
        grace: Duration::from_millis(300),
        ..Opts::default()
    }
}

/// The test binary itself as the child (and grandchild): hermetic, no
/// shell, no network. The name filter runs only `windows_supervise_helper`
/// and `--nocapture` sends its prints straight to the output pipe.
fn helper(mode: &str, dir: &Path) -> Command {
    let mut c = Command::new(std::env::current_exe().unwrap());
    c.arg("windows_supervise_helper")
        .arg("--nocapture")
        .env(HELPER_ENV, mode)
        .env(HELPER_DIR, dir);
    c
}

/// Started through [`helper`]: `sleep` just holds the pipe, `exit` exits
/// with code 7, `leaf` publishes its pid and sleeps, and `parent` starts a
/// `leaf` grandchild that inherits the pipe, publishes both pids and then
/// goes silent so the stall watchdog fires.
#[test]
#[serial]
fn windows_supervise_helper() {
    let Ok(mode) = std::env::var(HELPER_ENV) else {
        return; // plain test run: nothing to do
    };
    let dir = std::env::var(HELPER_DIR).unwrap_or_default();
    match mode.as_str() {
        "sleep" => {}
        "exit" => std::process::exit(7),
        "leaf" => write_pid(&dir, "leaf.pid"),
        "leak" => {
            let missing = || Command::new(Path::new(&dir).join("no-such-program.exe"));
            // The first failed spawn may pay one-time process setup.
            assert!(supervise(missing(), &quick()).is_err());
            let before = handle_count();
            for _ in 0..3 {
                assert!(supervise(missing(), &quick()).is_err());
            }
            let now = handle_count();
            std::fs::write(
                Path::new(&dir).join("handles"),
                format!("{before} -> {now}"),
            )
            .unwrap();
            std::process::exit(if now == before { 0 } else { 3 });
        }
        "parent" => {
            // The grandchild is killed with the job; waiting on it would
            // block until exactly that.
            #[allow(clippy::zombie_processes)]
            let _grandchild = helper("leaf", Path::new(&dir)).spawn().unwrap();
            // A loaded host can take seconds to start a third test binary.
            let deadline = Instant::now() + Duration::from_secs(20);
            while !Path::new(&dir).join("leaf.pid").exists() {
                assert!(Instant::now() < deadline, "grandchild never started");
                println!("waiting"); // keeps the stall window open
                std::thread::sleep(Duration::from_millis(50));
            }
            write_pid(&dir, "parent.pid");
            println!("ready"); // the last output: the stall window starts
        }
        other => panic!("unknown helper mode {other}"),
    }
    std::thread::sleep(Duration::from_secs(600));
}

/// Publish the helper's own pid for the test to inspect after the kill.
fn write_pid(dir: &str, name: &str) {
    std::fs::write(Path::new(dir).join(name), std::process::id().to_string()).unwrap();
}

fn read_pid(dir: &Path, name: &str) -> u32 {
    std::fs::read_to_string(dir.join(name))
        .unwrap()
        .trim()
        .parse()
        .unwrap()
}

fn process_alive(pid: u32) -> bool {
    // SAFETY: opens our own child's pid read-only; the handle is closed
    // again here on every path.
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return false;
        }
        let mut code = 0u32;
        let ok = GetExitCodeProcess(h, &mut code);
        CloseHandle(h);
        ok != 0 && code == STILL_ACTIVE as u32
    }
}

fn wait_until_gone(pid: u32) -> bool {
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if !process_alive(pid) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    false
}

fn handle_count() -> u32 {
    let mut n = 0u32;
    // SAFETY: the pseudo-handle of the running process; the count is
    // written into a local and the pseudo-handle is never closed.
    unsafe {
        GetProcessHandleCount(GetCurrentProcess(), &mut n);
    }
    n
}

#[test]
#[serial]
fn stall_kill_takes_down_child_and_grandchild() {
    let d = tempfile::tempdir().unwrap();
    let o = Opts {
        stall: Some(Duration::from_millis(300)),
        timeout: Some(Duration::from_secs(3)),
        ..quick()
    };
    let t = Instant::now();
    let r = supervise(helper("parent", d.path()), &o).unwrap();
    assert!(r.stalled && !r.timed_out, "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
    let parent = read_pid(d.path(), "parent.pid");
    let leaf = read_pid(d.path(), "leaf.pid");
    assert!(wait_until_gone(parent), "child {parent} still alive");
    assert!(wait_until_gone(leaf), "grandchild {leaf} still alive");
}

#[test]
#[serial]
fn timeout_terminates_the_child() {
    let d = tempfile::tempdir().unwrap();
    let o = Opts {
        timeout: Some(Duration::from_millis(500)),
        ..quick()
    };
    let t = Instant::now();
    let r = supervise(helper("sleep", d.path()), &o).unwrap();
    assert!(r.timed_out && !r.stalled && !r.cancelled, "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
}

#[test]
#[serial]
fn cancel_terminates_the_child() {
    let d = tempfile::tempdir().unwrap();
    let flag = Arc::new(AtomicBool::new(false));
    let f = flag.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(300));
        f.store(true, Ordering::Relaxed);
    });
    let t = Instant::now();
    let r = supervise_with_cancel(helper("sleep", d.path()), &quick(), &|| {
        flag.load(Ordering::Relaxed)
    })
    .unwrap();
    assert!(r.cancelled && !r.stalled && !r.timed_out, "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
}

#[test]
#[serial]
fn normal_exit_reports_the_exit_code() {
    let d = tempfile::tempdir().unwrap();
    let r = supervise(helper("exit", d.path()), &quick()).unwrap();
    assert_eq!(r.code, Some(7));
    assert_eq!(r.signal, None);
    assert!(!r.stalled && !r.timed_out && !r.cancelled, "{r:?}");
}

#[test]
#[serial]
fn failed_spawn_errors_without_leaking_a_job_handle() {
    // One warm-up and three sampled failed spawns, in a child process that
    // runs only the helper (see the module doc).
    let d = tempfile::tempdir().unwrap();
    let status = helper("leak", d.path()).status().unwrap();
    let counts = std::fs::read_to_string(d.path().join("handles")).unwrap_or_default();
    assert!(
        status.success(),
        "handles leaked over 3 failed spawns: {counts} ({status})"
    );
}

/// The child harness must get libtest's `--nocapture` (a misspelling makes
/// the harness reject the option and exit before the helper body runs).
#[test]
#[serial]
fn helper_passes_the_libtest_nocapture_flag() {
    let d = tempfile::tempdir().unwrap();
    let args: Vec<String> = helper("sleep", d.path())
        .get_args()
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    assert!(args.iter().any(|a| a == "--nocapture"), "{args:?}");
}

/// Caller-set creation flags are replaced by the supervisor's own pair; the
/// supervised child must still spawn, resume and report its exit code.
#[test]
#[serial]
fn caller_creation_flags_do_not_break_supervision() {
    let d = tempfile::tempdir().unwrap();
    let mut c = helper("exit", d.path());
    c.creation_flags(CREATE_NO_WINDOW);
    let r = supervise(c, &quick()).unwrap();
    assert_eq!(r.code, Some(7));
}

/// The locally declared snapshot row keeps the layout and size the walk
/// requires (seven fixed-width fields, `dwSize` first).
#[test]
#[serial]
fn local_snapshot_declarations_match_the_walk() {
    assert_eq!(std::mem::size_of::<THREADENTRY32>(), 28);
    assert_eq!(TH32CS_SNAPTHREAD, 0x4);
}
