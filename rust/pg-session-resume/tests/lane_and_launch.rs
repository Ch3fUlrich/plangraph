//! The lane copy of the latest stop and the launch identity of a stop (a respawn).

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, stop_done};
use serde_json::json;

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
    stop_done(&mut h, &r.cfg, 0, None); // signalled, never published: the sidecar of run-1
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
    assert!(!pending(&r).exists(), "and is removed");
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

fn order_rig(file: &str) -> (Rig, Fake) {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(&r, "lane-a.resume.json", file);
    (r, Fake::new(5000))
}

#[test]
fn a_stop_never_replaces_the_lane_copy_of_a_later_one() {
    // publication: the lane file already names a later stop of the lane
    let (r, mut h) = order_rig(
        r#"{"state":"stopped","session_name":"newer-0002","handle":"NEW","stopped_ts":9000,"launch_ts":1}"#,
    );
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert_eq!(lane_copy(&r)["handle"], "NEW");
    reap_idle(&mut h, &r.cfg);
    assert_eq!(lane_copy(&r)["handle"], "NEW", "repair keeps it too");
}

#[test]
fn stops_of_the_same_second_are_ordered_by_their_launch() {
    // this record: stopped_ts 5000, launch ts 1000 (the rig)
    let same = |launch: u64, name: &str, handle: &str| {
        format!(
            r#"{{"state":"stopped","session_name":"{name}","handle":"{handle}","stopped_ts":5000,"launch_ts":{launch}}}"#
        )
    };
    // an earlier launch stopped in the same second: replaced
    let (r, mut h) = order_rig(&same(900, "older-0001", "OLD"));
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(lane_copy(&r)["handle"], SID);
    // a later launch stopped in the same second: kept, in publication and in repair
    let (r, mut h) = order_rig(&same(1100, "later-0002", "LATER"));
    stop_done(&mut h, &r.cfg, 0, None);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(lane_copy(&r)["handle"], "LATER");
}
