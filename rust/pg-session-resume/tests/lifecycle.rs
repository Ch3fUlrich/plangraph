//! SESSION-AUTO-CLOSE done-when: DONE stops a session with its handle recorded; a
//! findings file resumes the same session id with exactly the file text; idle
//! sessions are stopped; the allowlist is never stopped; one log line per event.

mod common;

use common::*;
use pg_session_resume::engine::{reap_idle, resume_findings, stop_done};
use serde_json::json;

#[test]
fn done_stops_the_session_and_records_the_resume_handle() {
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
    assert_eq!(rec["resume"]["agent"], "claude");
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(
        h.lines[0].contains("event=stop name=writer-0644 age_s=4000 reason=done ram_freed_mb=1000"),
        "{}",
        h.lines[0]
    );
}

#[test]
fn status_without_done_or_head_does_not_stop() {
    let r = rig("writer", json!({}));
    write(
        &r,
        "lane-a.status",
        "DONE k abc\nHEAD: 0123456789abcdef0123456789abcdef01234567\n",
    );
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.status", "lane-a DONE\nHEAD: nothex\n");
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
fn stop_is_refused_without_a_resume_handle() {
    let r = rig("writer", json!({"session_id": null}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert!(h.lines[0].contains("event=stop-skipped") && h.lines[0].contains("no-resume-handle"));
    assert!(record(&r, "writer").get("resume").is_none());
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
fn findings_resume_the_same_session_with_exactly_the_file_text() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    // a round of an earlier review (another head), left from the hand-written days
    write(
        &r,
        "lane-a.fix-round-1.txt",
        "old round for an older head\n",
    );
    write_head(&r, 1, HEAD2);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1, "a round of another head is not sent");

    let text = "REQUEST_CHANGES\n  HIGH foo.rs:3 off by one\n\n";
    write(&r, "lane-a.fix-round-2.txt", text);
    h.now = 6000;
    resume_findings(&mut h, &r.cfg);
    let (cwd, argv) = h.runs.last().unwrap();
    assert_eq!(cwd.as_deref(), Some(r.wt.as_path()));
    assert_eq!(argv[..4], ["claude", "--bg", "--resume", SID]);
    assert_eq!(argv[4], text);
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "running");
    assert!(rec["stopped_ts"].is_null());
    assert_eq!(rec["resume"]["round"], 2);
    let line = resume_line(&h);
    assert!(
        line.contains("event=resume name=writer-0644")
            && line.contains("reason=findings")
            && line.contains("round=2")
            && line.contains("worktree=clean")
            && line.contains("stopped_for_s=1000"),
        "{line}"
    );

    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 2, "one findings file resumes once");
    assert!(h
        .lines
        .iter()
        .any(|l| l.contains("event=resume-acked") && l.contains("round=2")));
}

#[test]
fn the_old_done_line_does_not_stop_a_resumed_writer_but_a_new_head_does() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let runs = h.runs.len();
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.len(), runs, "stale DONE for the reviewed head");
    done(&r, HEAD2);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.len(), runs + 1);
    let rec = record(&r, "writer");
    assert_eq!(rec["resume"]["status_head"], HEAD2);
    assert_eq!(rec["resume"]["round"], 1, "the consumed round is kept");
}

#[test]
fn a_dirty_or_moved_worktree_is_resumed_but_the_log_says_so() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.dirty = Some(true);
    h.head = Some(HEAD2.into());
    resume_findings(&mut h, &r.cfg);
    assert!(
        resume_line(&h).contains("worktree=dirty+moved"),
        "{:?}",
        h.lines
    );
}

#[test]
fn low_ram_defers_the_resume_without_consuming_the_findings() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.mem = vec![100];
    h.mem_reads.set(0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(
        h.lines.last().unwrap().contains("event=resume-deferred")
            && h.lines.last().unwrap().contains("low-ram")
    );
    h.mem = vec![9000];
    h.mem_reads.set(0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 2);
}

#[test]
fn empty_and_oversized_findings_are_never_sent() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", " \n");
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    write(&r, "lane-a.fix-round-2.txt", &"x".repeat(100_001));
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(h.lines.last().unwrap().contains("event=resume-refused"));
    resume_findings(&mut h, &r.cfg);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("resume-refused"))
            .count(),
        1,
        "refused once"
    );
}

#[test]
fn codex_and_opencode_resume_detached_by_their_own_handle() {
    for (agent, key, want) in [
        ("codex", "thread_id", ["codex", "exec", "resume"]),
        (
            "opencode",
            "opencode_session",
            ["opencode", "run", "--session"],
        ),
    ] {
        let r = rig(
            "writer",
            json!({"agent": agent, key: "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
        );
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        h.procs.insert(4242, 9);
        stop_done(&mut h, &r.cfg, 60, None);
        assert_eq!(h.runs[0].1, ["kill", "4242"]);
        assert_eq!(record(&r, "writer")["resume"]["handle"], "T-1");
        write(&r, "lane-a.fix-round-1.txt", "fix it");
        resume_findings(&mut h, &r.cfg);
        assert_eq!(h.spawns[0][..3], want);
        assert_eq!(h.spawns[0][3..], ["T-1", "fix it"], "{agent}");
    }
}

#[test]
fn idle_for_thirty_minutes_stops_and_a_shorter_idle_does_not() {
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
}

#[test]
fn allowlisted_sessions_are_never_reaped() {
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    for name in ["build-L1", "plan-order"] {
        let r = rig(name, json!({}));
        reap_idle(&mut h, &r.cfg);
        assert!(h.runs.is_empty(), "{name} is on the allowlist");
    }
}

#[test]
fn a_findings_round_the_session_has_not_acknowledged_keeps_it_running() {
    let r = rig("writer", json!({}));
    write(&r, "lane-a.fix-round-1.txt", "old\n");
    let mut h = Fake::new(100_000);
    h.activity = Some(95_000);
    reap_idle(&mut h, &r.cfg);
    assert!(
        h.runs.is_empty() && h.lines.is_empty(),
        "round 1 is pending work"
    );
    // unrelated activity never clears it: only the acknowledged round does
    h.activity = Some(99_999);
    h.now = 200_000;
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty());
    patch_resume(&r, "writer", json!({"round": 1}));
    reap_idle(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn a_stopped_record_is_not_stopped_twice() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 1);
}
