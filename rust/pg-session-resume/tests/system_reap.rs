//! The reaper against the real host: a per-session activity file and turn-end marker, a real
//! process, a real `kill`. Own test binary: it forks.

use pg_session_resume::engine::{reap_idle, Config};
use pg_session_resume::host::Host;
use pg_session_resume::record::Record;
use pg_session_resume::system::{proc_start_ticks, System};
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
        min_free_mb: 0,
        allow: vec![],
    };
    Rig { dir, child, cfg }
}

fn host(r: &Rig) -> System {
    System {
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
fn a_process_holds_the_prompt_only_while_it_is_alive_with_it_in_its_arguments() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let prompt = "fix the thing\nplease";
    let mut child = Command::new("sh")
        .args(["-c", "sleep 30", "launch", prompt])
        .spawn()
        .unwrap();
    let start = h.launch_start(child.id()).unwrap();
    // between fork and exec the child still shows its parent's command line: wait for exec
    for _ in 0..200 {
        if h.process_holds_prompt(child.id(), start, prompt) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(h.process_holds_prompt(child.id(), start, prompt));
    assert!(
        !h.process_holds_prompt(child.id(), start, "another round"),
        "not this round's text"
    );
    assert!(
        !h.process_holds_prompt(child.id(), start + 1, prompt),
        "not this process"
    );
    child.kill().unwrap(); // killed, not waited for: a zombie with its start time still readable
    for _ in 0..100 {
        if h.proc_start(child.id()).is_none() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    assert_eq!(h.proc_start(child.id()), None, "a zombie is gone");
    assert_eq!(
        h.launch_start(child.id()),
        Some(start),
        "but its launch identity is still readable"
    );
    assert!(!h.process_holds_prompt(child.id(), start, prompt));
    child.wait().unwrap();
}

#[test]
fn newer_activity_in_the_same_second_as_the_turn_end_marker_keeps_the_turn_open() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
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

#[test]
fn a_resumed_process_is_found_from_its_command_line_among_the_non_process_proc_entries() {
    let dir = tempfile::tempdir().unwrap();
    let h = System {
        log_file: dir.path().join("log"),
        log_failed: false,
    };
    let handle = format!("T-find-{}", std::process::id());
    let mut child = Command::new("sh")
        .args(["-c", "sleep 30", "launch", "resume", &handle])
        .spawn()
        .unwrap();
    let mut found = None;
    for _ in 0..200 {
        found = h.find_process(&handle);
        if found.is_some() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert_eq!(found.map(|(p, _)| p), Some(child.id()));
    assert_eq!(h.find_process("T-nobody-has-this"), None);
    child.kill().unwrap();
    child.wait().unwrap();
}
