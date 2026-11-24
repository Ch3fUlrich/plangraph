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
        tasks: Some(tasks),
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
    // every refusal has its reason, logged once however often the tick repeats
    for (state, tasks, why) in [
        ("done", Some(2), "job-has-tasks"),
        ("done", None, "job-tasks-unknown"),
        ("working", Some(0), "job-working"),
        ("blocked", Some(0), "job-blocked"),
    ] {
        let r = rig("writer", json!({}));
        done(&r, HEAD);
        let mut h = Fake::new(100_000);
        h.activity = Some(1);
        h.job = Some(JobState {
            state: state.into(),
            tasks,
        });
        for _ in 0..3 {
            stop_done(&mut h, &r.cfg, 0, None);
            reap_idle(&mut h, &r.cfg);
        }
        assert!(h.runs.is_empty(), "{state} {tasks:?}");
        assert_eq!(h.lines.len(), 1, "{state} {tasks:?}: {:?}", h.lines);
        assert!(h.lines[0].contains(&format!("why={why}")), "{}", h.lines[0]);
        assert!(record(&r, "writer").get("resume").is_none());
    }
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
        assert_eq!(h.terminated, [(4242, 9)], "{agent}");
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
    assert!(
        h.terminated.is_empty() && h.runs.is_empty(),
        "{:?}",
        h.terminated
    );
    assert_eq!(record(&r, "writer")["state"], "stopped");

    // without a recorded identity nothing is signalled either
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242}),
    );
    done(&r, HEAD);
    h.procs.insert(4242, 9);
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.terminated.is_empty());
    assert!(h.lines.last().unwrap().contains("why=no-process-identity"));
}

#[test]
fn codex_without_a_turn_end_signal_or_with_children_is_kept() {
    for (turn, children, why) in [
        (None, Some(false), "no-idle-signal"),
        (Some(false), Some(false), "turn-open"),
        (Some(true), Some(true), "has-children"),
        (Some(true), None, "no-runtime-state"),
    ] {
        let r = rig("writer", codex(9));
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        h.procs.insert(4242, 9);
        (h.turn_ended, h.children) = (turn, children);
        for _ in 0..3 {
            stop_done(&mut h, &r.cfg, 0, None);
            reap_idle(&mut h, &r.cfg);
        }
        assert!(h.terminated.is_empty());
        assert_eq!(h.lines.len(), 1, "{why}: {:?}", h.lines);
        assert!(h.lines[0].contains(&format!("why={why}")), "{}", h.lines[0]);
        assert!(record(&r, "writer").get("resume").is_none());
    }
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

#[test]
fn an_unreadable_process_is_not_proof_of_exit_and_a_survivor_is_not_stopped() {
    let r = rig("writer", codex(9));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.proc_unknown = true;
    stop_done(&mut h, &r.cfg, 0, None);
    assert!(h.terminated.is_empty());
    assert!(h.lines[0].contains("why=no-runtime-state"), "{:?}", h.lines);
    assert!(record(&r, "writer").get("resume").is_none());

    // SIGTERM was sent but the process stays alive: nothing is published as stopped
    h.proc_unknown = false;
    h.survives = true;
    h.procs.insert(4242, 9);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.terminated, [(4242, 9)]);
    assert!(h.lines.last().unwrap().contains("event=stop-failed"));
    let rec = record(&r, "writer");
    assert!(rec.get("resume").is_none() && rec["state"] != "stopped");
    assert!(!r.cfg.pr_dir.join("lane-a.resume.json").exists());
}

#[test]
fn protected_names_of_the_python_reaper_are_never_stopped_by_either_path() {
    for name in [
        "auto-planner-v2",
        "Auto-Planner",
        "plangraph-oversight-seat",
        "PlanGraph-Oversight-9",
        "a-judge-b",
        "routing-0007",
        "x-L1-y",
    ] {
        let mut r = rig(name, json!({}));
        r.cfg.allow = pg_session_resume::engine::DEFAULT_ALLOW
            .iter()
            .map(|s| (*s).to_owned())
            .collect();
        done(&r, HEAD);
        let mut h = Fake::new(100_000);
        h.activity = Some(1);
        reap_idle(&mut h, &r.cfg);
        stop_done(&mut h, &r.cfg, 0, None);
        assert!(h.runs.is_empty() && h.lines.is_empty(), "{name}");
    }
}

#[test]
fn a_record_that_cannot_be_saved_is_not_published_and_is_recovered_without_a_second_stop() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let rec_path = r.cfg.launch_dir.join("writer-0644.json");
    let tmp = rec_path.with_extension("json.tmp");
    std::fs::create_dir(&tmp).unwrap(); // the atomic rename's temp file cannot be created
    let mut h = Fake::new(5000);
    assert_eq!(
        stop_done(&mut h, &r.cfg, 0, None),
        1,
        "reported as a failure"
    );
    assert_eq!(h.runs.len(), 1);
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=record-save-failed")));
    assert!(
        !h.lines.iter().any(|l| l.contains("event=stop ")),
        "{:?}",
        h.lines
    );
    assert!(!r.cfg.pr_dir.join("lane-a.resume.json").exists());
    assert!(record(&r, "writer").get("resume").is_none());

    std::fs::remove_dir(&tmp).unwrap();
    h.job = job("stopped", 0); // the runtime did stop it
    assert_eq!(stop_done(&mut h, &r.cfg, 0, None), 0);
    assert_eq!(h.runs.len(), 1, "no second stop command");
    assert_eq!(record(&r, "writer")["resume"]["handle"], SID);
    assert!(h.lines.last().unwrap().contains("event=stop "));
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
    let lane: serde_json::Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(lane["handle"], SID);
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
    let mut rec: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
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
