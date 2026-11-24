//! Finishing a stop whose writes failed: the phases of the sidecar and what each one proves.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, stop_done};
use serde_json::json;

fn attempt_snapshot(r: &Rig) {
    let snap = json!({"agent": "claude", "handle": SID, "reason": "done", "head": HEAD,
        "worktree": "w1", "status_head": HEAD, "round": 0, "stopped_ts": 4000,
        "launch_ts": 1000, "launch_run": null, "phase": "attempt"});
    std::fs::write(pending(r), snap.to_string()).unwrap();
}

#[test]
fn an_attempt_whose_session_is_stopped_is_published_as_unconfirmed() {
    let r = rig("writer", json!({}));
    attempt_snapshot(&r);
    let mut h = Fake::new(5000);
    h.job = stopped_job();
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "stopped");
    assert_eq!(rec["stopped_ts"], 4000);
    assert_eq!(
        (
            rec["resume"]["head"].as_str(),
            rec["resume"]["unconfirmed"].as_bool()
        ),
        (Some(HEAD), Some(true))
    );
    assert!(!pending(&r).exists());
    assert_eq!(lane_copy(&r)["handle"], SID);
}

#[test]
fn an_attempt_on_an_unreadable_runtime_is_kept_and_reported_once() {
    let r = rig("writer", json!({}));
    attempt_snapshot(&r);
    let mut h = Fake::new(5000);
    h.job = None;
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    assert!(pending(&r).exists(), "the only account of the stop stays");
    let unconfirmed: Vec<_> = h
        .lines
        .iter()
        .filter(|l| l.contains("event=stop-unconfirmed"))
        .collect();
    assert_eq!(unconfirmed.len(), 1, "{:?}", h.lines);
    h.job = stopped_job(); // readable again
    reap_idle(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["unconfirmed"], true);
    assert!(!pending(&r).exists());
}

#[test]
fn a_stop_whose_phase_write_and_record_write_both_fail_is_still_recorded() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.json.tmp"));
    h.also_block_on_run = Some(r.cfg.launch_dir.join("writer-0644.pending.tmp"));
    assert!(stop_done(&mut h, &r.cfg, 0, None) >= 1, "reported");
    assert_eq!(
        read(&pending(&r))["phase"],
        "attempt",
        "no evidence reached the disk"
    );
    // later: both writes work again, DONE is gone, the runtime shows the session stopped
    for f in ["writer-0644.json.tmp", "writer-0644.pending.tmp"] {
        std::fs::remove_dir(r.cfg.launch_dir.join(f)).unwrap();
    }
    std::fs::remove_file(r.cfg.pr_dir.join("lane-a.status")).unwrap();
    h.block_on_run = None;
    h.also_block_on_run = None;
    h.job = stopped_job();
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["state"].as_str(), rec["resume"]["head"].as_str()),
        (Some("stopped"), Some(HEAD))
    );
    assert_eq!(h.runs.len(), 1, "no second stop");
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1
    );
}

#[test]
fn a_stop_line_whose_logged_phase_could_not_be_written_is_not_repeated() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    let blocker = r.cfg.launch_dir.join("writer-0644.pending.tmp");
    h.block_on_log = Some(blocker.clone()); // the phase write after the append fails
    assert_eq!(stop_done(&mut h, &r.cfg, 0, None), 1);
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1
    );
    assert_eq!(read(&pending(&r))["phase"], "stopped");
    std::fs::remove_dir(&blocker).unwrap();
    h.block_on_log = None;
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    assert_eq!(
        h.lines.iter().filter(|l| l.contains("event=stop ")).count(),
        1,
        "the line is known by the stop's id: {:?}",
        h.lines
    );
    assert!(!pending(&r).exists());
}

#[test]
fn a_job_that_turns_busy_while_the_activity_is_read_again_is_not_stopped() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.busy_on_activity_read = Some(2); // the recheck right before dispatch (quiet 0: the Stop hook)
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty(), "{:?}", h.runs);
    assert!(
        h.lines.iter().any(|l| l.contains("why=job-working")),
        "{:?}",
        h.lines
    );
    assert!(!pending(&r).exists());
}

#[test]
fn an_attempt_whose_record_is_published_but_whose_stop_line_failed_is_logged_by_the_next_tick() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    // the phase write after the stop fails, the record write works, the log append fails
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.pending.tmp"));
    h.log_fails = true;
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert_eq!(read(&pending(&r))["phase"], "attempt");
    std::fs::remove_dir(r.cfg.launch_dir.join("writer-0644.pending.tmp")).unwrap();
    h.block_on_run = None;
    h.log_fails = false;
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    let stops: Vec<_> = h
        .lines
        .iter()
        .filter(|l| l.contains("event=stop "))
        .collect();
    assert_eq!(stops.len(), 1, "{:?}", h.lines);
    assert!(!pending(&r).exists());
    assert_eq!(h.runs.len(), 1, "no second stop");
}

#[test]
fn a_launch_with_an_unresolved_attempt_gets_no_fresh_stop_in_the_same_tick() {
    let r = rig("writer", json!({}));
    attempt_snapshot(&r);
    let before = std::fs::read_to_string(pending(&r)).unwrap();
    let mut h = Fake::new(9000);
    h.activity = Some(1); // idle for hours: eligible for a fresh stop
    h.job_unreadable_reads.set(1); // unreadable for recovery, readable for the stop loop
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty(), "{:?}", h.runs);
    assert_eq!(
        std::fs::read_to_string(pending(&r)).unwrap(),
        before,
        "snapshot untouched"
    );
    assert!(record(&r, "writer").get("resume").is_none());
}
