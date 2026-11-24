//! A round is delivered on the runtime's receipt for this launch, and only for a verified head.

mod common;

use common::*;
use pg_session_resume::engine::{resume_findings, stop_done};
use serde_json::json;

fn stopped_codex(h: &mut Fake) -> Rig {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    h.procs.insert(4242, 9);
    done(&r, HEAD);
    stop_done(h, &r.cfg, 60, None);
    r
}

#[test]
fn a_live_process_without_the_prompt_in_its_arguments_is_no_receipt() {
    let mut h = Fake::new(5000);
    let r = stopped_codex(&mut h);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.tcontains = None;
    h.holds_prompt = false; // alive, but it was not started with this round's text
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(rec["resume"]["round"], 0);
    assert_eq!(
        rec["resume"]["claim_round"], 1,
        "still waiting for a receipt"
    );
    h.holds_prompt = true;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn a_session_that_exited_after_taking_the_prompt_keeps_its_identity_and_is_stopped_later() {
    let mut h = Fake::new(5000);
    let r = stopped_codex(&mut h);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.spawn_dies = true; // codex recorded the prompt and exited before the next tick
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
    // the writer's next DONE (new head) records the session as stopped without signalling anyone
    let kills = h.runs.len();
    done(&r, HEAD2);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.len(), kills, "nothing to kill");
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["state"].as_str(), rec["resume"]["status_head"].as_str()),
        (Some("stopped"), Some(HEAD2))
    );
    // and the next review of that head reaches it
    pg_session_resume::deliver::deliver(&r.cfg.pr_dir, "lane-a", HEAD2, "round two").unwrap();
    h.spawn_dies = false;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.spawns.last().unwrap()[4], "round two");
}

#[test]
fn a_missing_or_malformed_lane_status_never_lets_a_round_through() {
    for broken in [None, Some(""), Some("lane-a DONE\nHEAD: not-a-sha\n")] {
        let r = rig("writer", json!({}));
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        stop_done(&mut h, &r.cfg, 60, None);
        write(&r, "lane-a.fix-round-1.txt", "review of A");
        match broken {
            None => std::fs::remove_file(r.cfg.pr_dir.join("lane-a.status")).unwrap(),
            Some(text) => write(&r, "lane-a.status", text),
        }
        resume_findings(&mut h, &r.cfg);
        resume_findings(&mut h, &r.cfg);
        assert_eq!(h.runs.len(), 1, "{broken:?}");
        assert_eq!(
            h.lines
                .iter()
                .filter(|l| l.contains("why=no-lane-head"))
                .count(),
            1,
            "{broken:?}"
        );
        done(&r, HEAD); // the status is readable again: the round goes out
        resume_findings(&mut h, &r.cfg);
        assert_eq!(h.runs.last().unwrap().1[4], "review of A");
    }
}

#[test]
fn a_live_launcher_that_ignores_its_prompt_is_no_receipt() {
    let mut h = Fake::new(5000);
    let r = stopped_codex(&mut h);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.tcontains = None;
    h.mark_frozen = true; // the process holds the text in its arguments but the session writes nothing
    resume_findings(&mut h, &r.cfg);
    h.now = 6000 + 10_000; // far past the grace: the process is alive, so no second writer either
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (
            rec["resume"]["round"].as_u64(),
            rec["resume"]["claim_round"].as_u64()
        ),
        (Some(0), Some(1))
    );
    assert_eq!(h.spawns.len(), 1, "one launch");
    h.mark_frozen = false; // the session shows activity
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

/// A tick died after the spawn and before it saved the pid: the claim has no process.
fn interrupted_after_spawn(h: &mut Fake) -> Rig {
    let r = stopped_codex(h);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    patch_resume(
        &r,
        "writer",
        json!({"claim_round": 1, "claim_ts": 6000, "claim_offset": 100, "claim_mark": 7}),
    );
    h.procs.insert(900, 5); // the writer the dead tick started
    h.find = Some((900, 5));
    h.tcontains = None;
    h.now = 6050;
    r
}

#[test]
fn a_tick_that_died_after_the_spawn_recovers_the_process_before_it_acknowledges() {
    let mut h = Fake::new(5000);
    let r = interrupted_after_spawn(&mut h);
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (
            rec["resume"]["round"].as_u64(),
            rec["pid"].as_u64(),
            rec["pid_start"].as_u64()
        ),
        (Some(1), Some(900), Some(5))
    );
    assert!(h.spawns.is_empty(), "no second writer");
}

#[test]
fn a_tick_that_died_after_the_spawn_never_retries_beside_the_live_writer() {
    let mut h = Fake::new(5000);
    let r = interrupted_after_spawn(&mut h);
    h.mark_frozen = true; // not acknowledged yet
    h.now = 6000 + 10_000; // way past the grace
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.spawns.is_empty() && h.runs.len() == 1,
        "no retry while the original is alive"
    );
    let rec = record(&r, "writer");
    assert_eq!(
        (
            rec["resume"]["claim_pid"].as_u64(),
            rec["resume"]["claim_round"].as_u64()
        ),
        (Some(900), Some(1))
    );
}

#[test]
fn a_status_with_a_valid_head_followed_by_a_malformed_one_authorizes_nothing() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "review of A");
    write(
        &r,
        "lane-a.status",
        &format!("lane-a DONE\nHEAD: {HEAD}\nHEAD: 12345\n"),
    );
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(h.lines.iter().any(|l| l.contains("why=no-lane-head")));
}
