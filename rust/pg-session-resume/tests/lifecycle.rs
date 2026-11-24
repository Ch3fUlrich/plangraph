//! SESSION-CLOSE-STOP-AND-REAP done-when: DONE stops a session with its handle recorded
//! only on a positive idle signal; idle sessions are reaped; the allowlist and sessions
//! without a handle are never stopped; one log line per stop.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, stop_done};
use pg_session_resume::host::JobState;
use serde_json::json;

const HEAD2: &str = "fedcba9876543210fedcba9876543210fedcba98";

fn job(state: &str, tasks: u64) -> Option<JobState> {
    Some(JobState {
        state: state.into(),
        tasks,
    })
}

#[test]
fn done_stops_with_handle_recorded() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs[0].1, ["claude", "stop", "1591af3e"]);
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "stopped");
    assert_eq!(rec["stopped_ts"], 5000);
    assert_eq!(rec["resume"]["handle"], SID);
    assert_eq!(rec["resume"]["head"], HEAD);
    assert_eq!(rec["resume"]["worktree"], "w1");
    assert_eq!(rec["resume"]["status_head"], HEAD);
    assert_eq!(rec["resume"]["round"], 0);
    assert_eq!(rec["resume"]["agent"], "claude");
    let lane: serde_json::Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(
        (lane["state"].as_str(), lane["handle"].as_str()),
        (Some("stopped"), Some(SID))
    );
}

#[test]
fn one_log_line_per_stop() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    stop_done(&mut h, &r.cfg, 60, None);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(
        h.lines[0].contains("event=stop name=writer-0644 age_s=4000 reason=done ram_freed_mb=1000"),
        "{}",
        h.lines[0]
    );
    assert!(h.lines[0].contains(&format!("handle={SID}")));
}

#[test]
fn status_without_done_or_a_valid_last_head_does_not_stop() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(5000);
    write(&r, "lane-a.status", &format!("DONE k abc\nHEAD: {HEAD}\n"));
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.status", "lane-a DONE\nHEAD: nothex\n");
    stop_done(&mut h, &r.cfg, 60, None);
    // a half-rewritten status clears the earlier valid head instead of authorizing it
    write(
        &r,
        "lane-a.status",
        &format!("lane-a DONE\nHEAD: {HEAD}\nHEAD: 0123\n"),
    );
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty() && h.lines.is_empty());
}

#[test]
fn a_session_still_writing_is_not_stopped_by_the_timer_path() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.activity = Some(4990);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    stop_done(&mut h, &r.cfg, 0, None); // the Stop hook path: the turn has ended
    assert_eq!(h.runs.len(), 1);
}

#[test]
fn only_the_named_session_is_stopped_by_the_hook_path() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 0, Some("someone-else"));
    assert!(h.runs.is_empty());
    stop_done(&mut h, &r.cfg, 0, Some(SID));
    assert_eq!(h.runs.len(), 1);
}

#[test]
fn no_handle_never_stopped() {
    let r = rig("writer", json!({"session_id": null}));
    done(&r, HEAD);
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    stop_done(&mut h, &r.cfg, 60, None);
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty());
    assert_eq!(h.lines.len(), 1, "refused once: {:?}", h.lines);
    assert!(h.lines[0].contains("event=stop-skipped") && h.lines[0].contains("no-resume-handle"));
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn a_handle_that_is_not_a_plain_id_is_no_handle() {
    let r = rig("writer", json!({"session_id": "x; rm -rf"}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty());
}

#[test]
fn claude_job_with_tasks_is_kept() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    for (state, tasks) in [("done", 2), ("working", 0), ("blocked", 0)] {
        h.job = job(state, tasks);
        stop_done(&mut h, &r.cfg, 0, None);
        reap_idle(&mut h, &r.cfg);
    }
    assert!(h.runs.is_empty() && h.lines.is_empty(), "{:?}", h.lines);
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn an_unreadable_claude_job_state_keeps_the_session_and_logs_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = None;
    stop_done(&mut h, &r.cfg, 0, None);
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty());
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(h.lines[0].contains("why=no-job-state"));
}

#[test]
fn a_claude_job_the_runtime_already_stopped_only_gets_its_record() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = job("stopped", 0);
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty());
    assert_eq!(record(&r, "writer")["state"], "stopped");
}

#[test]
fn a_failed_stop_command_leaves_the_record_untouched() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.fail_run = true;
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(record(&r, "writer").get("resume").is_none());
    assert!(h.lines[0].contains("event=stop-failed"));
}

#[test]
fn idle_30min_stops_and_allowlist_never() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(10_000);
    h.activity = Some(10_000 - 1799);
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty());
    h.activity = Some(10_000 - 1800);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(
        h.lines[0].contains("event=stop name=writer-0644") && h.lines[0].contains("reason=idle"),
        "{}",
        h.lines[0]
    );
    assert_eq!(record(&r, "writer")["resume"]["handle"], SID);

    // live L1s, routing, planners and overseers by their counter names are never stopped,
    // neither by the reaper nor by a DONE line
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    for name in [
        "build-L1",
        "build-L1-0412",
        "plan-order",
        "plan-roadmap-0003",
        "routing-0007",
        "overseer-0001",
        "planner-0002",
        "L1-0001",
    ] {
        let mut r = rig(name, json!({}));
        r.cfg.allow = pg_session_resume::engine::DEFAULT_ALLOW
            .iter()
            .map(|s| (*s).to_owned())
            .collect();
        done(&r, HEAD);
        reap_idle(&mut h, &r.cfg);
        stop_done(&mut h, &r.cfg, 0, None);
        assert!(h.runs.is_empty(), "{name} is on the allowlist");
    }
}

#[test]
fn a_stopped_record_is_not_stopped_twice() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    done(&r, HEAD);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 1);
}

fn codex(pid_start: u64) -> serde_json::Value {
    json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": pid_start})
}

#[test]
fn codex_and_opencode_stop_by_pid_with_their_own_handle() {
    for (agent, key) in [("codex", "thread_id"), ("opencode", "opencode_session")] {
        let r = rig(
            "writer",
            json!({"agent": agent, key: "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
        );
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        h.procs.insert(4242, 9);
        stop_done(&mut h, &r.cfg, 60, None);
        assert_eq!(h.runs[0].1, ["kill", "4242"], "{agent}");
        assert_eq!(record(&r, "writer")["resume"]["handle"], "T-1");
    }
}

#[test]
fn pid_start_mismatch_never_signalled() {
    let r = rig("writer", codex(9));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 10); // another process holds the pid now
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty(), "{:?}", h.runs);
    assert_eq!(record(&r, "writer")["state"], "stopped");

    // without a recorded identity nothing is signalled either
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242}),
    );
    done(&r, HEAD);
    h.procs.insert(4242, 9);
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty());
    assert!(h.lines.last().unwrap().contains("why=no-process-identity"));
}

#[test]
fn codex_without_a_turn_end_signal_or_with_children_is_kept() {
    let r = rig("writer", codex(9));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 9);
    h.turn_ended = None;
    stop_done(&mut h, &r.cfg, 0, None);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(h.lines[0].contains("why=no-idle-signal"));
    h.turn_ended = Some(false); // a turn is open
    stop_done(&mut h, &r.cfg, 0, None);
    h.turn_ended = Some(true);
    h.children = Some(true); // a tool is running
    stop_done(&mut h, &r.cfg, 0, None);
    h.children = None; // unreadable
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.runs.is_empty(), "{:?}", h.runs);
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn the_reaper_records_the_head_of_the_lane_status_as_status_head() {
    let r = rig("writer", json!({}));
    write(&r, "lane-a.status", &format!("HEAD: {HEAD2}\n"));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["status_head"], HEAD2);
}
