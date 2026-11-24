//! What survives a failed write or a respawn: the stop snapshot, the lane copy of the latest
//! stop, the launch identity of a stop, the retry of a refusal line that could not be logged.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, stop_done};
use pg_session_resume::host::JobState;
use serde_json::{json, Value};

const HEAD2: &str = "fedcba9876543210fedcba9876543210fedcba98";

fn read(path: &std::path::Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn lane_copy(r: &Rig) -> Value {
    read(&r.cfg.pr_dir.join("lane-a.resume.json"))
}

fn stopped_job() -> Option<JobState> {
    Some(JobState {
        state: "stopped".into(),
        tasks: Some(0),
    })
}

#[test]
fn a_record_that_cannot_be_saved_before_the_signal_is_never_signalled() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let tmp = r.cfg.launch_dir.join("writer-0644.json.tmp");
    std::fs::create_dir(&tmp).unwrap(); // the atomic rename's temp file cannot be created
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
    assert!(rec.get("resume").is_none() && rec["stop_pending"]["head"] == HEAD);

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
    assert!(rec["stop_pending"].is_null());
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
    assert!(record(&r, "writer")["stop_pending"].is_null());
}

#[test]
fn a_lane_copy_that_failed_is_repaired_by_a_later_tick() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let blocker = r.cfg.pr_dir.join("lane-a.resume.json.tmp");
    std::fs::create_dir(&blocker).unwrap();
    let mut h = Fake::new(5000);
    assert_eq!(stop_done(&mut h, &r.cfg, 0, None), 1);
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=lane-record-failed")));
    std::fs::remove_dir(&blocker).unwrap();
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    assert_eq!(h.runs.len(), 1, "the stop is not repeated");
    assert_eq!(lane_copy(&r)["handle"], SID);
}

#[test]
fn a_lane_copy_of_an_older_launch_is_replaced_by_the_latest_stop() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    // the lane file names an earlier launch of the lane, stopped before this one
    write(
        &r,
        "lane-a.resume.json",
        r#"{"lane":"lane-a","state":"stopped","session_name":"older-0001","handle":"OLD","stopped_ts":100}"#,
    );
    // the write of the new copy fails once
    let blocker = r.cfg.pr_dir.join("lane-a.resume.json.tmp");
    std::fs::create_dir(&blocker).unwrap();
    let mut h = Fake::new(5000);
    assert_eq!(stop_done(&mut h, &r.cfg, 0, None), 1);
    assert_eq!(lane_copy(&r)["handle"], "OLD");
    std::fs::remove_dir(&blocker).unwrap();
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    let lane = lane_copy(&r);
    assert_eq!(
        (lane["handle"].as_str(), lane["stopped_ts"].as_u64()),
        (Some(SID), Some(5000))
    );
    // a copy of a newer stop is never overwritten by an older one
    write(
        &r,
        "lane-a.resume.json",
        r#"{"lane":"lane-a","state":"stopped","session_name":"newer-0002","handle":"NEW","stopped_ts":9000}"#,
    );
    reap_idle(&mut h, &r.cfg);
    assert_eq!(lane_copy(&r)["handle"], "NEW");
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
fn a_respawned_launch_is_not_hidden_by_the_stop_of_the_old_one() {
    let r = rig("writer", json!({"run_id": "run-1"}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 1);
    // `fleet-launch --respawn` copies the record: stop marks and resume object stay, the launch
    // identity (ts, run id, session id) is new
    let path = r.cfg.launch_dir.join("writer-0644.json");
    let mut rec = read(&path);
    rec["ts"] = json!(6000);
    rec["run_id"] = json!("run-2");
    rec["session_id"] = json!("2222222a-91d1-442a-bc81-f5dd71769181");
    rec["job_id"] = json!("2222222a");
    std::fs::write(&path, rec.to_string()).unwrap();
    h.now = 7000;
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 2, "the new launch is a live session again");
    assert_eq!(h.runs[1].1, ["claude", "stop", "2222222a"]);
    assert_eq!(record(&r, "writer")["resume"]["launch_run"], "run-2");
}

#[test]
fn a_pending_snapshot_of_the_old_launch_is_not_used_by_a_respawn() {
    let r = rig("writer", json!({"run_id": "run-1"}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.block_on_run = Some(r.cfg.launch_dir.join("writer-0644.json.tmp"));
    stop_done(&mut h, &r.cfg, 0, None); // signalled, never published: stop_pending of run-1
    std::fs::remove_dir(r.cfg.launch_dir.join("writer-0644.json.tmp")).unwrap();
    let path = r.cfg.launch_dir.join("writer-0644.json");
    let mut rec = read(&path);
    rec["ts"] = json!(6000);
    rec["run_id"] = json!("run-2");
    std::fs::write(&path, rec.to_string()).unwrap();
    h.block_on_run = None;
    h.job = stopped_job();
    assert_eq!(reap_idle(&mut h, &r.cfg), 0);
    assert!(
        record(&r, "writer").get("resume").is_none(),
        "the old launch's snapshot says nothing about the new one"
    );
}

#[test]
fn a_foreign_stop_mark_older_than_the_launch_does_not_hide_it() {
    let r = rig(
        "writer",
        json!({"state": "stopped", "stopped_ts": 900, "ts": 1000}),
    );
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 1);
}
