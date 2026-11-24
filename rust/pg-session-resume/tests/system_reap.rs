//! The reaper against the real host: a per-session activity file and turn-end marker, a real
//! process, a real `kill`. Own test binary: it forks.

use pg_session_resume::engine::{reap_idle, Config};
use pg_session_resume::host::{Host, Process};
use pg_session_resume::record::Record;
use pg_session_resume::system::{parse_job_state, proc_start_ticks, System};
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
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let mut child = sh("trap '' TERM; while :; do sleep 1; done");
    std::thread::sleep(std::time::Duration::from_millis(300)); // the trap is installed
    let err = h.terminate(child.id(), start_of(child.id())).unwrap_err();
    assert!(err.contains("still running"), "{err}");
    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn process_tells_live_from_gone() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
        exit_wait_ms: 0,
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let mut child = sh("exit 0");
    let pid = child.id();
    child.wait().unwrap();
    assert_eq!(h.process(pid), Process::Gone);
    assert!(matches!(h.process(std::process::id()), Process::Live(_)));
}

#[test]
fn a_task_count_that_is_not_a_number_is_never_read_as_zero() {
    let tasks = |text: &str| parse_job_state(text).map(|j| (j.state, j.tasks));
    let done = |flight: &str| format!(r#"{{"state":"done","inFlight":{flight}}}"#);
    assert_eq!(
        tasks(&done(
            r#"{"tasks":0,"queued":0,"kinds":[],"drainableMonitors":0}"#
        )),
        Some(("done".into(), Some(0)))
    );
    assert_eq!(
        tasks(&done(r#"{"tasks":2}"#)),
        Some(("done".into(), Some(2)))
    );
    assert_eq!(
        tasks(&done(r#"{"tasks":0,"queued":1}"#)),
        Some(("done".into(), Some(1)))
    );
    for bad in [
        r#"{"tasks":"2"}"#,
        r#"{"tasks":-1}"#,
        r#"{"tasks":null}"#,
        r#"{"queued":0}"#,
        r#"{"tasks":0,"queued":"1"}"#,
        r#"{"tasks":0,"drainableMonitors":[]}"#,
        "null",
        "7",
    ] {
        assert_eq!(tasks(&done(bad)), Some(("done".into(), None)), "{bad}");
    }
    assert_eq!(tasks(r#"{"state":"done"}"#), Some(("done".into(), None)));
    assert_eq!(tasks("{"), None);
    assert_eq!(tasks(r#"{"state":5}"#), None);
}
