//! What survives a failed write or a respawn: the stop snapshot, the lane copy of the latest
//! stop, the launch identity of a stop, the retry of a refusal line that could not be logged.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, stop_done};
use pg_session_resume::host::JobState;
use serde_json::json;

const HEAD2: &str = "fedcba9876543210fedcba9876543210fedcba98";

#[test]
fn a_record_that_cannot_be_saved_before_the_signal_is_never_signalled() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let tmp = r.cfg.launch_dir.join("writer-0644.pending.tmp");
    std::fs::create_dir(&tmp).unwrap(); // the snapshot cannot be written
    let mut h = Fake::new(5000);
    assert_eq!(
        stop_done(&mut h, &r.cfg, 0, None),
        1,
        "a failure is reported"
    );
    assert!(
        h.runs.is_empty(),
        "nothing is stopped that cannot be remembered"
    );
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=record-save-failed")));
    assert!(!h.lines.iter().any(|l| l.contains("event=stop ")));
}

#[test]
fn a_stop_whose_record_write_fails_is_finished_from_the_snapshot_taken_before_it() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    // the stop command runs, then the record cannot be written any more
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.json.tmp"));
    assert_eq!(
        stop_done(&mut h, &r.cfg, 0, None),
        1,
        "reported as a failure"
    );
    assert_eq!(h.runs.len(), 1);
    assert!(
        !h.lines.iter().any(|l| l.contains("event=stop ")),
        "{:?}",
        h.lines
    );
    assert!(!r.cfg.pr_dir.join("lane-a.resume.json").exists());
    let rec = record(&r, "writer");
    assert!(rec.get("resume").is_none());
    assert_eq!(read(&pending(&r))["head"], HEAD);
    assert_eq!(
        read(&pending(&r))["phase"],
        "stopped",
        "the stop command succeeded"
    );

    // later: the worktree moved on, the status is gone, the runtime shows the session stopped
    std::fs::remove_dir(r.cfg.launch_dir.join("writer-0644.json.tmp")).unwrap();
    std::fs::remove_file(r.cfg.pr_dir.join("lane-a.status")).unwrap();
    h.block_on_run = None;
    h.head = Some(HEAD2.into());
    h.job = stopped_job();
    h.now = 9000;
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    assert_eq!(h.runs.len(), 1, "no second stop command");
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "stopped");
    assert_eq!(rec["stopped_ts"], 5000, "the moment of the stop");
    assert_eq!(
        rec["resume"]["head"], HEAD,
        "the worktree of the stop, not of the retry"
    );
    assert_eq!(rec["resume"]["status_head"], HEAD);
    assert_eq!(rec["resume"]["reason"], "done");
    assert!(!pending(&r).exists(), "finished stops leave no sidecar");
    assert_eq!(lane_copy(&r)["handle"], SID);
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1,
        "{:?}",
        h.lines
    );
}

#[test]
fn a_stop_that_failed_leaves_no_snapshot_behind() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.fail_run = true;
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(!pending(&r).exists());
}

#[test]
fn a_refusal_whose_log_line_could_not_be_appended_is_logged_by_the_next_tick() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = Some(JobState {
        state: "working".into(),
        tasks: Some(0),
    });
    h.log_fails = true;
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(
        record(&r, "writer")["stop_refused"].is_null(),
        "not remembered unlogged"
    );
    h.log_fails = false;
    stop_done(&mut h, &r.cfg, 0, None);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(h.lines[0].contains("why=job-working"));
}

#[test]
fn a_session_that_starts_a_turn_while_the_snapshot_is_taken_is_not_signalled() {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    h.busy_on_head = true; // the worktree lookup is when the turn starts
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.terminated.is_empty(), "{:?}", h.terminated);
    assert!(record(&r, "writer").get("resume").is_none());
    assert!(!pending(&r).exists());
    assert!(
        h.lines.iter().any(|l| l.contains("why=turn-open")),
        "{:?}",
        h.lines
    );
}

#[test]
fn an_aborted_attempt_whose_snapshot_cannot_be_removed_is_reported() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.fail_run = true;
    h.jam_on_run = Some(pending(&r)); // the sidecar can no longer be removed
    assert_eq!(
        stop_done(&mut h, &r.cfg, 0, None),
        1,
        "reported as a failure"
    );
    assert!(
        h.lines
            .iter()
            .any(|l| l.contains("event=stop-pending-left")),
        "{:?}",
        h.lines
    );
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn a_stop_line_that_could_not_be_appended_is_written_by_the_next_tick() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.log_fails = true;
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert!(pending(&r).exists(), "kept until the line is in the log");
    h.log_fails = false;
    h.now = 9000;
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    reap_idle(&mut h, &r.cfg);
    let stops: Vec<_> = h
        .lines
        .iter()
        .filter(|l| l.contains("event=stop "))
        .collect();
    assert_eq!(stops.len(), 1, "{:?}", h.lines);
    assert!(stops[0].contains("reason=done") && stops[0].contains(&format!("handle={SID}")));
    assert!(!pending(&r).exists());
    assert_eq!(h.runs.len(), 1, "no second stop");
}

#[test]
fn a_snapshot_left_by_an_attempt_that_never_finished_is_not_published_after_a_later_exit() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    // a tick died after writing its snapshot and before the stop command
    let snap = json!({"agent": "claude", "handle": SID, "reason": "done", "head": HEAD,
        "worktree": "w1", "status_head": HEAD, "round": 0, "stopped_ts": 4000,
        "launch_ts": 1000, "launch_run": null});
    std::fs::write(pending(&r), snap.to_string()).unwrap();
    reap_idle(&mut h, &r.cfg); // the session runs (job done, not stopped by anyone)
    assert!(!pending(&r).exists(), "an unfinished attempt is dropped");
    h.job = stopped_job(); // it exits on its own later
    reap_idle(&mut h, &r.cfg);
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn a_stop_that_happened_is_finished_even_when_the_runtime_state_cannot_be_read() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.json.tmp"));
    stop_done(&mut h, &r.cfg, 0, None); // stopped, record not written
    std::fs::remove_dir(r.cfg.launch_dir.join("writer-0644.json.tmp")).unwrap();
    std::fs::remove_file(r.cfg.pr_dir.join("lane-a.status")).unwrap();
    h.block_on_run = None;
    h.job = None; // the job state cannot be read now
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["state"].as_str(), rec["resume"]["head"].as_str()),
        (Some("stopped"), Some(HEAD))
    );
    assert!(!pending(&r).exists());
}

#[test]
fn a_stop_line_already_in_the_log_is_not_written_again() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.json.tmp"));
    stop_done(&mut h, &r.cfg, 0, None);
    std::fs::remove_dir(r.cfg.launch_dir.join("writer-0644.json.tmp")).unwrap();
    h.block_on_run = None;
    // the record is published and the line logged, then only the sidecar's removal failed
    reap_idle(&mut h, &r.cfg);
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1
    );
    let mut snap = read(&r.cfg.launch_dir.join("writer-0644.json"))["resume"].clone();
    snap["phase"] = json!("logged");
    std::fs::write(pending(&r), snap.to_string()).unwrap();
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1,
        "{:?}",
        h.lines
    );
    assert!(!pending(&r).exists(), "only the removal was left");
}

#[test]
fn a_session_that_was_active_again_during_the_snapshot_is_not_stopped() {
    // reaping: the idle check used the activity read before the worktree lookup
    let r = rig("writer", json!({}));
    let mut h = Fake::new(10_000);
    h.activity = Some(1); // idle for hours ...
    h.active_after_head = Some(9_990); // ... until a turn ran while the snapshot was taken
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty());
    assert!(
        h.lines.iter().any(|l| l.contains("why=activity-resumed")),
        "{:?}",
        h.lines
    );
    assert!(!pending(&r).exists());
    assert!(record(&r, "writer").get("resume").is_none());
    // the DONE path: quiet for 60 s before, active 5 s ago after the lookup
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(10_000);
    h.activity = Some(1);
    h.active_after_head = Some(9_995);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
}

#[test]
fn what_a_stop_writes_is_readable_by_its_owner_only_whatever_the_umask() {
    use std::os::unix::fs::PermissionsExt;
    // SAFETY: umask only widens what this process creates; other tests do not depend on it.
    unsafe { libc::umask(0) };
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    // stale temp files of an earlier crash, created world-readable
    for stale in [
        r.cfg.launch_dir.join("writer-0644.json.tmp"),
        r.cfg.launch_dir.join("writer-0644.pending.tmp"),
        r.cfg.pr_dir.join("lane-a.resume.json.tmp"),
    ] {
        std::fs::write(&stale, "old").unwrap();
        std::fs::set_permissions(&stale, std::fs::Permissions::from_mode(0o644)).unwrap();
    }
    let mut h = Fake::new(5000);
    h.log_fails = true; // the sidecar stays behind to be looked at
    stop_done(&mut h, &r.cfg, 0, None);
    for path in [
        pending(&r),
        r.cfg.launch_dir.join("writer-0644.json"),
        r.cfg.pr_dir.join("lane-a.resume.json"),
    ] {
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "{}", path.display());
    }
}
