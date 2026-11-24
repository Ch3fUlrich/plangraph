//! Delivery is the runtime's acknowledgement, never a claim, a clock or a process start.

mod common;

use common::*;
use pg_session_resume::engine::{resume_findings, stop_done};
use serde_json::{json, Value};

/// A stopped record with a claim a dead tick left for round 1 (claimed at 6000).
fn dead_claim(r: &Rig, h: &mut Fake, extra: Value) {
    done(r, HEAD);
    stop_done(h, &r.cfg, 60, None);
    let mut claim = json!({"claim_round": 1, "claim_ts": 6000, "claim_offset": 100});
    claim
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    patch_resume(r, "writer", claim);
    write(r, "lane-a.fix-round-1.txt", "fix it");
}

fn launches(h: &Fake) -> usize {
    h.runs
        .iter()
        .filter(|(_, a)| a.get(1).map(String::as_str) == Some("--bg"))
        .count()
}

#[test]
fn a_claim_counts_as_sent_only_when_the_transcript_holds_the_prompt() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    dead_claim(&r, &mut h, json!({}));
    h.now = 7000;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(launches(&h), 0, "acknowledged: no second launch");
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["resume"]["round"].as_u64(), rec["state"].as_str()),
        (Some(1), Some("running"))
    );
    assert!(rec["resume"]["claim_round"].is_null());
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=resume-acked") && l.contains("round=1")));
}

#[test]
fn unrelated_activity_never_acknowledges_a_claim() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    dead_claim(&r, &mut h, json!({}));
    h.tcontains = Some(false); // the transcript grew, but not with this prompt
    h.activity = Some(6999);
    h.now = 6050; // inside the grace: a Claude job may still be starting
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(rec["resume"]["round"], 0, "not delivered");
    assert_eq!(rec["resume"]["claim_round"], 1, "the claim waits");
    assert_eq!(launches(&h), 0);
}

#[test]
fn an_unacknowledged_claim_past_its_grace_is_dropped_and_the_round_sent_again() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    dead_claim(&r, &mut h, json!({}));
    h.tcontains = Some(false);
    h.now = 6000 + 121;
    resume_findings(&mut h, &r.cfg);
    assert!(h.lines.iter().any(|l| l.contains("outcome=not-acked")));
    assert_eq!(launches(&h), 1, "the lost round goes out now");
    assert_eq!(h.runs.last().unwrap().1[4], "fix it");
    assert_eq!(
        record(&r, "writer")["resume"]["claim_round"],
        1,
        "a fresh claim"
    );
}

#[test]
fn a_started_process_is_not_a_delivery_until_the_prompt_is_acknowledged() {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    h.turn_ended = Some(true);
    done(&r, HEAD);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.tcontains = Some(false); // the rollout has not shown the prompt yet
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        rec["resume"]["round"], 0,
        "process creation alone is no delivery"
    );
    assert_eq!(
        (
            rec["resume"]["claim_pid"].as_u64(),
            rec["resume"]["claim_start"].as_u64()
        ),
        (Some(777), Some(55))
    );
    h.tcontains = Some(true); // the next tick sees it
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (
            rec["resume"]["round"].as_u64(),
            rec["pid"].as_u64(),
            rec["pid_start"].as_u64()
        ),
        (Some(1), Some(777), Some(55))
    );
}

#[test]
fn a_runtime_without_a_transcript_acknowledges_by_the_live_process_it_started() {
    let r = rig(
        "writer",
        json!({"agent": "opencode", "opencode_session": "S-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    done(&r, HEAD);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.tcontains = None;
    h.tlen = None;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
    // and a process that died before it could be seen is not a delivery
    let r = rig(
        "writer",
        json!({"agent": "opencode", "opencode_session": "S-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    done(&r, HEAD);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.tcontains = None;
    h.spawn_dies = true; // the new process is gone again when it is looked at
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 0);
}

#[test]
fn the_process_of_a_dead_tick_is_found_again_from_its_command_line() {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    dead_claim(&r, &mut h, json!({}));
    h.find = Some((900, 5));
    h.now = 7000;
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (
            rec["pid"].as_u64(),
            rec["pid_start"].as_u64(),
            rec["resume"]["round"].as_u64()
        ),
        (Some(900), Some(5), Some(1))
    );
}

#[test]
fn findings_for_a_head_the_lane_has_moved_past_are_refused() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "review of A");
    done(&r, HEAD2); // the lane status advanced to B after the stop at A
    resume_findings(&mut h, &r.cfg);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("why=head-superseded"))
            .count(),
        1,
        "{:?}",
        h.lines
    );
}

#[test]
fn a_status_touched_after_a_finding_never_marks_it_delivered() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(&r, "lane-a.fix-round-1.txt", "arrived after DONE");
    touch(&r, "lane-a.fix-round-1.txt", 10_050);
    done(&r, HEAD); // the writer rewrites the same DONE after the finding: newer mtime
    touch(&r, "lane-a.status", 10_100);
    let mut h = Fake::new(20_000);
    stop_done(&mut h, &r.cfg, 60, None);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "arrived after DONE");
}

#[test]
fn an_unreadable_status_file_does_not_consume_any_round() {
    let r = rig("writer", json!({}));
    write(&r, "lane-a.fix-round-1.txt", "waiting");
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    pg_session_resume::engine::reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty(), "pending, so kept");
    assert!(record(&r, "writer").get("resume").is_none());
}
