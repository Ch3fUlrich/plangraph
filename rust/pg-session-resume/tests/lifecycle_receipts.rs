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
