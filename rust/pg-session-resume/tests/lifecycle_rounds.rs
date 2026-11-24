//! Fix rounds 1 and 2 of SESSION-AUTO-CLOSE: delivery, claims, runtime state, bindings.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, resume_findings, stop_done};
use pg_session_resume::host::JobState;
use serde_json::{json, Value};

// ---- fix round 1 ----

fn j(state: &str, tasks: u64) -> Option<JobState> {
    Some(JobState {
        state: state.into(),
        tasks,
    })
}

#[test]
fn a_findings_file_between_done_and_the_stop_is_still_sent() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(&r, "lane-a.fix-round-1.txt", "arrived before the stop\n");
    touch(&r, "lane-a.fix-round-1.txt", 10_050);
    let mut h = Fake::new(20_000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(record(&r, "writer")["resume"]["round"], 0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "arrived before the stop\n");
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn only_the_newest_stopped_record_of_a_lane_resumes_and_a_live_one_blocks() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    add_record(
        &r,
        "writer-0700",
        json!({"session_id": "2222222a-91d1-442a-bc81-f5dd71769181", "state": "stopped", "stopped_ts": 4000,
        "resume": {"agent": "claude", "handle": "2222222a-91d1-442a-bc81-f5dd71769181", "head": HEAD, "status_head": HEAD, "round": 0, "stopped_ts": 4000}}),
    );
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.now = 6000;
    resume_findings(&mut h, &r.cfg);
    let resumes: Vec<_> = h
        .runs
        .iter()
        .filter(|(_, a)| a.get(1).map(String::as_str) == Some("--bg"))
        .collect();
    assert_eq!(resumes.len(), 1, "one file, one launch");
    assert_eq!(
        resumes[0].1[3], SID,
        "the record stopped last (5000 > 4000) wins"
    );

    // a live record of the lane blocks every stopped one
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    add_record(&r, "writer-0800", json!({}));
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
}

#[test]
fn findings_for_another_head_are_refused_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "late finding for head A");
    write_head(&r, 1, HEAD2); // the review was of another head than this session stopped at
    resume_findings(&mut h, &r.cfg);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("why=head-mismatch"))
            .count(),
        1,
        "{:?}",
        h.lines
    );
}

#[test]
fn the_round_is_claimed_before_the_launch_and_given_back_when_it_fails() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.fail_run = true;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(
        record(&r, "writer")["resume"]["round"],
        0,
        "a failed launch gives the round back"
    );
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert!(h.lines.last().unwrap().contains("event=resume-failed"));
    h.fail_run = false;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn a_working_job_with_an_old_transcript_is_not_stopped() {
    for (state, tasks) in [("working", 0), ("blocked", 0), ("done", 2)] {
        let r = rig("writer", json!({}));
        let mut h = Fake::new(100_000);
        h.activity = Some(1);
        h.job = j(state, tasks);
        reap_idle(&mut h, &r.cfg);
        done(&r, HEAD);
        stop_done(&mut h, &r.cfg, 0, None);
        assert!(h.runs.is_empty(), "{state}/{tasks}");
        assert!(h.lines.is_empty() && record(&r, "writer").get("resume").is_none());
    }
}

#[test]
fn a_job_the_runtime_already_stopped_only_gets_its_resume_handle_recorded() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = j("stopped", 0);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert_eq!(record(&r, "writer")["resume"]["handle"], SID);
    assert!(h.lines[0].contains("event=stop name=writer-0644"));
}

#[test]
fn an_unreadable_job_state_keeps_the_session_and_logs_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = None;
    stop_done(&mut h, &r.cfg, 60, None);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(h.lines[0].contains("why=no-job-state"));
}

#[test]
fn a_reused_pid_is_never_signalled_and_a_resume_stores_the_new_identity() {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 10); // another process now owns the pid
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty(), "no kill of a stranger");
    assert_eq!(record(&r, "writer")["resume"]["handle"], "T-1");

    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["pid"].as_u64(), rec["pid_start"].as_u64()),
        (Some(777), Some(55))
    );

    // the next stop checks the new identity
    done(&r, HEAD2);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.last().unwrap().1, ["kill", "777"]);
}

#[test]
fn a_non_claude_record_without_process_identity_is_not_stopped() {
    for extra in [
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242}),
        json!({"agent": "opencode", "opencode_session": "S-1", "session_id": null}),
    ] {
        let r = rig("writer", extra);
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        stop_done(&mut h, &r.cfg, 60, None);
        assert!(h.runs.is_empty());
        assert!(h.lines[0].contains("why=no-process-identity"));
    }
}

#[test]
fn an_idle_stop_without_a_status_head_keeps_the_git_head_for_the_moved_check() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["head"], HEAD);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.head = Some(HEAD2.into());
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=clean+moved"),
        "{:?}",
        h.lines
    );
}

#[test]
fn a_failed_dirty_check_is_logged_as_unknown_never_clean() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.dirty = None;
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=unknown"),
        "{:?}",
        h.lines
    );
}

#[test]
fn the_lane_gets_a_resume_file_at_the_stop_and_at_the_resume() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    let lane: Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(
        (
            lane["state"].as_str(),
            lane["handle"].as_str(),
            lane["head"].as_str()
        ),
        (Some("stopped"), Some(SID), Some(HEAD))
    );
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let lane: Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(
        (lane["state"].as_str(), lane["round"].as_u64()),
        (Some("running"), Some(1))
    );
}

// ---- fix round 2 ----

#[test]
fn findings_without_a_reviewed_head_sidecar_are_refused_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    std::fs::write(r.cfg.pr_dir.join("lane-a.fix-round-1.txt"), "fix it").unwrap();
    resume_findings(&mut h, &r.cfg);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("why=no-reviewed-head"))
            .count(),
        1
    );
    write_head(&r, 1, HEAD); // the producer adds the sidecar: the same file now goes out
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "fix it");
}

#[test]
fn a_finding_in_the_same_instant_as_the_done_line_is_sent_not_lost() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(&r, "lane-a.fix-round-1.txt", "same second");
    touch(&r, "lane-a.fix-round-1.txt", 10_000); // exactly the mtime of the DONE status
    let mut h = Fake::new(20_000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(record(&r, "writer")["resume"]["round"], 0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "same second");
}

fn dead_claim(r: &Rig, h: &mut Fake) {
    done(r, HEAD);
    stop_done(h, &r.cfg, 60, None);
    let path = r.cfg.launch_dir.join("writer-0644.json");
    let mut rec: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    rec["resume"]["claim_round"] = json!(1);
    rec["resume"]["claim_ts"] = json!(6000);
    std::fs::write(&path, rec.to_string()).unwrap();
    write(r, "lane-a.fix-round-1.txt", "fix it");
}

#[test]
fn a_claim_left_by_a_dead_tick_counts_as_sent_when_the_runtime_shows_life_after_it() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    dead_claim(&r, &mut h);
    h.activity = Some(6500);
    h.now = 7000;
    let runs = h.runs.len();
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), runs, "no second launch");
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["resume"]["round"].as_u64(), rec["state"].as_str()),
        (Some(1), Some("running"))
    );
    assert!(rec["resume"]["claim_round"].is_null());
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=resume-reconciled") && l.contains("outcome=launched")));
}

#[test]
fn a_claim_left_by_a_dead_tick_is_dropped_and_the_round_sent_again_when_nothing_ran() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    dead_claim(&r, &mut h);
    h.activity = Some(5500); // before the claim at 6000
    h.now = 7000;
    resume_findings(&mut h, &r.cfg);
    assert!(h.lines.iter().any(|l| l.contains("outcome=not-launched")));
    assert_eq!(
        h.runs.last().unwrap().1[4],
        "fix it",
        "the lost round goes out now"
    );
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn a_codex_session_with_a_running_child_or_unknown_state_is_never_stopped() {
    for (children, why) in [(Some(true), None), (None, Some("no-runtime-state"))] {
        let r = rig(
            "writer",
            json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
        );
        let mut h = Fake::new(100_000);
        h.activity = Some(1);
        h.procs.insert(4242, 9);
        h.children = children;
        reap_idle(&mut h, &r.cfg);
        done(&r, HEAD);
        stop_done(&mut h, &r.cfg, 0, None);
        assert!(h.runs.is_empty(), "{children:?}");
        assert_eq!(
            h.lines
                .iter()
                .filter(|l| why.is_some_and(|w| l.contains(w)))
                .count(),
            usize::from(why.is_some())
        );
    }
}

#[test]
fn a_replaced_worktree_at_the_same_commit_is_logged_and_an_unknown_baseline_is_not_clean() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.wt_id = Some("w2".into()); // same commit, another checkout
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=clean+replaced"),
        "{:?}",
        h.lines
    );

    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.head = None;
    h.wt_id = None; // both lookups failed at the stop
    stop_done(&mut h, &r.cfg, 60, None);
    let rec = record(&r, "writer");
    assert!(
        rec["resume"]["head"].is_null() && rec["resume"]["worktree"].is_null(),
        "no substitute for a failed lookup"
    );
    h.head = Some(HEAD.into());
    h.wt_id = Some("w1".into());
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let line = h.lines.last().unwrap();
    assert!(
        line.contains("worktree=clean+head-unknown+worktree-unknown"),
        "{line}"
    );
}
