//! The reaper against the real host: a per-session activity file and turn-end marker, a real
//! process, a real `kill`. Own test binary: it forks.

#[path = "../../test_support/python.rs"]
mod test_python;

use pg_session_resume::engine::{reap_idle, Config};
use pg_session_resume::host::{Host, Process};
use pg_session_resume::record::Record;
use pg_session_resume::system::{classify, proc_start_ticks, System};
use serde_json::{json, Value};
use std::path::Path;
use std::process::{Child, Command};

fn set_mtime(path: &Path, secs: u64) {
    let f = std::fs::File::options().write(true).open(path).unwrap();
    f.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(secs))
        .unwrap();
}

struct Rig {
    dir: tempfile::TempDir,
    child: Child,
    cfg: Config,
}

fn rig(turn_end_secs: u64) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let (launch, pr) = (dir.path().join("sessions"), dir.path().join("pr"));
    for d in [&launch, &pr, &dir.path().join("lane-a")] {
        std::fs::create_dir_all(d).unwrap();
    }
    let child = Command::new("sleep").arg("30").spawn().unwrap();
    let start =
        proc_start_ticks(&std::fs::read_to_string(format!("/proc/{}/stat", child.id())).unwrap())
            .unwrap();
    let (activity, turn_end) = (dir.path().join("activity"), dir.path().join("turn-end"));
    for f in [&activity, &turn_end] {
        std::fs::write(f, "x").unwrap();
    }
    set_mtime(&activity, 1_000_000);
    set_mtime(&turn_end, turn_end_secs);
    let rec = json!({"name": "writer", "session_name": "writer-1", "cwd": dir.path().join("lane-a"), "agent": "opencode",
        "opencode_session": "S-1", "pid": child.id(), "pid_start": start, "ts": 1,
        "activity_path": activity, "turn_end_path": turn_end});
    std::fs::write(launch.join("writer-1.json"), rec.to_string()).unwrap();
    let cfg = Config {
        launch_dir: launch,
        pr_dir: pr,
        idle_s: 1800,
        allow: vec![],
    };
    Rig { dir, child, cfg }
}

fn host(r: &Rig) -> System {
    System {
        exit_wait_ms: 5000,
        proc_root: "/proc".into(),
        log_file: r.dir.path().join("log"),
        log_failed: false,
    }
}

fn record(r: &Rig) -> Value {
    serde_json::from_slice(&std::fs::read(r.cfg.launch_dir.join("writer-1.json")).unwrap()).unwrap()
}

#[test]
fn an_idle_session_with_a_turn_end_marker_after_its_activity_is_stopped_and_recorded() {
    let mut r = rig(1_000_100); // the turn ended after the last activity
    reap_idle(&mut host(&r), &r.cfg);
    let status = r.child.wait().unwrap(); // `kill` ended the real process
    assert!(!status.success());
    let rec = record(&r);
    assert_eq!(
        (
            rec["state"].as_str(),
            rec["resume"]["handle"].as_str(),
            rec["resume"]["reason"].as_str()
        ),
        (Some("stopped"), Some("S-1"), Some("idle"))
    );
    assert!(std::fs::read_to_string(r.dir.path().join("log"))
        .unwrap()
        .contains("event=stop name=writer-1"));
}

#[test]
fn a_session_whose_turn_is_still_open_is_never_killed() {
    let mut r = rig(999_000); // marker older than the last activity: a turn is open
    reap_idle(&mut host(&r), &r.cfg);
    assert!(r.child.try_wait().unwrap().is_none(), "still running");
    assert!(record(&r).get("resume").is_none());
    r.child.kill().unwrap();
    r.child.wait().unwrap();
}

#[test]
fn a_session_without_a_turn_end_marker_is_kept_and_the_reason_logged_once() {
    let mut r = rig(1_000_100);
    let path = r.cfg.launch_dir.join("writer-1.json");
    let mut rec = record(&r);
    rec.as_object_mut().unwrap().remove("turn_end_path");
    std::fs::write(&path, rec.to_string()).unwrap();
    let mut h = host(&r);
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    assert!(r.child.try_wait().unwrap().is_none());
    let log = std::fs::read_to_string(r.dir.path().join("log")).unwrap();
    assert_eq!(log.matches("why=no-idle-signal").count(), 1, "{log}");
    assert!(h.now() > 0);
    r.child.kill().unwrap();
    r.child.wait().unwrap();
}

#[test]
fn same_instant_turn_marker_is_no_signal() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
        exit_wait_ms: 5000,
        proc_root: "/proc".into(),
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let (activity, marker) = (dir.path().join("activity"), dir.path().join("marker"));
    for f in [&activity, &marker] {
        std::fs::write(f, "x").unwrap();
    }
    let at = |p: &Path, ms: u64| {
        let f = std::fs::File::options().write(true).open(p).unwrap();
        f.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_millis(ms))
            .unwrap();
    };
    let rec = Record {
        path: dir.path().join("r.json"),
        value: json!({"agent": "opencode", "opencode_session": "S-1", "activity_path": activity, "turn_end_path": marker}),
    };
    at(&marker, 1_000_000_100); // the turn ended ...
    at(&activity, 1_000_000_900); // ... and a new one started within the same second
    assert_eq!(h.turn_ended(&rec), Some(false));
    at(&activity, 1_000_000_100); // the same instant proves nothing either
    assert_eq!(h.turn_ended(&rec), Some(false));
    at(&marker, 1_000_000_900);
    at(&activity, 1_000_000_100);
    assert_eq!(h.turn_ended(&rec), Some(true));
}

fn sh(script: &str) -> Child {
    Command::new("sh").args(["-c", script]).spawn().unwrap()
}

fn start_of(pid: u32) -> u64 {
    proc_start_ticks(&std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap()).unwrap()
}

#[test]
fn terminate_signals_only_the_recorded_process_and_waits_for_its_exit() {
    let dir = tempfile::tempdir().unwrap();
    let mut h = System {
        exit_wait_ms: 5000,
        proc_root: "/proc".into(),
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let mut child = sh("sleep 30");
    let start = start_of(child.id());
    // another start time: the pid might be a reused one, nothing is signalled
    assert!(h.terminate(child.id(), start + 1).is_err());
    assert!(child.try_wait().unwrap().is_none(), "still running");
    h.terminate(child.id(), start).unwrap();
    assert!(!child.wait().unwrap().success(), "ended by SIGTERM");
    assert_eq!(h.terminate(child.id(), start), Ok(()), "gone is stopped");
}

#[test]
fn a_process_that_survives_sigterm_is_a_failed_stop() {
    let dir = tempfile::tempdir().unwrap();
    let mut h = System {
        exit_wait_ms: 300,
        proc_root: "/proc".into(),
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let mut child = Command::new("/bin/sh")
        .args(["-c", "trap '' TERM; : > ready; exec /bin/sleep 30"])
        .current_dir(dir.path())
        .spawn()
        .unwrap();
    use std::time::{Duration, Instant};
    let deadline = Instant::now() + Duration::from_secs(15);
    let ready = dir.path().join("ready");
    while !ready.is_file() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    let result = ready
        .is_file()
        .then(|| h.terminate(child.id(), start_of(child.id())));
    child.kill().unwrap();
    child.wait().unwrap();
    let err = result.expect("fixture trap was not installed").unwrap_err();
    assert!(err.contains("still running"), "{err}");
}

#[test]
fn process_tells_live_from_gone() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
        exit_wait_ms: 0,
        proc_root: "/proc".into(),
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let mut child = sh("exit 0");
    let pid = child.id();
    child.wait().unwrap();
    assert_eq!(h.process(pid), Process::Gone);
    assert!(matches!(h.process(std::process::id()), Process::Live(_)));
}

/// A process whose main thread has exited (a zombie leader) while another thread still runs;
/// `ignore_term`: its signal disposition ignores SIGTERM.
fn zombie_leader_with_a_live_thread(ignore_term: bool) -> Child {
    let ignore = if ignore_term {
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    } else {
        ""
    };
    let script = format!(
        "import ctypes, threading, time, signal\n{ignore}\
         threading.Thread(target=time.sleep, args=(30,)).start()\n\
         ctypes.CDLL(None).pthread_exit(None)"
    );
    let child = test_python::command()
        .args(["-c", &script])
        .spawn()
        .unwrap();
    let is_zombie = || {
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", child.id())).unwrap();
        pg_session_resume::system::proc_state(&stat) == Some('Z')
    };
    for _ in 0..300 {
        if is_zombie() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(is_zombie(), "the leader did not become a zombie");
    child
}

fn system(exit_wait_ms: i32) -> (tempfile::TempDir, System) {
    let dir = tempfile::tempdir().unwrap();
    let host = System {
        exit_wait_ms,
        proc_root: "/proc".into(),
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    (dir, host)
}

#[test]
fn a_zombie_leader_with_a_live_thread_is_still_a_running_process() {
    let (_dir, mut h) = system(3000);
    let mut child = zombie_leader_with_a_live_thread(false);
    let start = start_of(child.id());
    assert_eq!(
        h.process(child.id()),
        Process::Live(start),
        "not gone while a thread runs"
    );
    // the stop is confirmed by the whole thread group's exit, not by the leader's state
    h.terminate(child.id(), start).unwrap();
    assert_eq!(h.process(child.id()), Process::Gone);
    child.wait().unwrap();
}

#[test]
fn a_zombie_leader_whose_thread_ignores_sigterm_is_not_a_stop() {
    let (_dir, mut h) = system(300);
    let mut child = zombie_leader_with_a_live_thread(true);
    let err = h.terminate(child.id(), start_of(child.id())).unwrap_err();
    assert!(err.contains("still running"), "{err}");
    child.kill().unwrap();
    child.wait().unwrap();
}

/// A `/proc/<pid>/stat` text with `state` and start time `start` (field 22).
fn stat_text(state: char, start: u64) -> String {
    let mut fields = vec!["0".to_owned(); 22];
    fields[0] = state.to_string();
    fields[19] = start.to_string();
    format!("4242 (x y) {}", fields.join(" "))
}

#[test]
fn a_zombie_leader_is_gone_only_when_the_kernel_says_every_thread_exited() {
    // The stat text alone (a zombie leader whose threads a /proc scan would call dead) decides
    // nothing: a process that is still running stays Live whatever the text says ...
    let mut running = sh("sleep 30");
    assert_eq!(
        classify(&stat_text('Z', 777), running.id()),
        Process::Live(777)
    );
    assert_eq!(
        classify(&stat_text('X', 777), running.id()),
        Process::Live(777)
    );
    assert_eq!(
        classify(&stat_text('S', 777), running.id()),
        Process::Live(777)
    );
    // ... and a process the kernel no longer knows is gone.
    running.kill().unwrap();
    running.wait().unwrap();
    assert_eq!(classify(&stat_text('Z', 777), running.id()), Process::Gone);
    assert_eq!(classify("garbage", running.id()), Process::Unknown);
}

#[test]
fn a_zombie_leader_stays_live_while_the_kernel_says_so_whatever_a_task_listing_shows() {
    // a made-up /proc: the leader is a zombie and its task listing holds only a zombie, which a
    // scan of the listing would call dead; the process is in fact alive
    let dir = tempfile::tempdir().unwrap();
    let mut running = sh("sleep 30");
    let pid = running.id();
    let task = dir.path().join(format!("{pid}/task/{pid}"));
    std::fs::create_dir_all(&task).unwrap();
    std::fs::write(dir.path().join(format!("{pid}/stat")), stat_text('Z', 777)).unwrap();
    std::fs::write(task.join("stat"), stat_text('Z', 777)).unwrap();
    let (_log, mut h) = system(300);
    h.proc_root = dir.path().to_owned();
    assert_eq!(h.process(pid), Process::Live(777));
    running.kill().unwrap();
    running.wait().unwrap();
    assert_eq!(h.process(pid), Process::Gone, "the kernel knows it exited");
}

#[test]
fn a_stop_id_is_matched_as_whole_fields_not_as_a_prefix() {
    let (dir, h) = system(0);
    let line = "ts=1 event=stop name=w-1 age_s=5 reason=done ram_freed_mb=1 handle=H1 stopped_ts=1000 launch_ts=7\n";
    std::fs::write(dir.path().join("log"), line).unwrap();
    let id = |h: &'static str, at: &'static str, l: &'static str| {
        [("handle", h), ("stopped_ts", at), ("launch_ts", l)]
    };
    assert!(h.log_has("stop", &id("H1", "1000", "7")));
    assert!(
        !h.log_has("stop", &id("H1", "100", "7")),
        "a stop time that is a prefix"
    );
    assert!(
        !h.log_has("stop", &id("H", "1000", "7")),
        "a handle that is a prefix"
    );
    assert!(
        !h.log_has("stop", &id("H1", "1000", "70")),
        "another launch"
    );
    assert!(
        !h.log_has("stop-skipped", &id("H1", "1000", "7")),
        "another event"
    );
}

#[test]
fn nothing_in_the_host_reads_the_real_proc_but_through_its_root() {
    // one root for every /proc read, so a scan and the kernel's answer can be given the same tree
    let src = include_str!("../src/system.rs");
    assert_eq!(src.matches("\"/proc").count(), 0, "a hard-coded /proc path");
}
