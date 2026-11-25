//! Round accounting of reviews: the stop rule, and reviews whose only findings are about the
//! review prompt (the run is held, not fixed).
use super::*;

#[test]
fn round_stop_is_the_round_progress_rule_capped_at_max_rounds() {
    assert_eq!(round_stop(&[3]), None);
    assert_eq!(round_stop(&[3, 2, 1, 0]), None);
    assert_eq!(round_stop(&[3, 3, 3]), Some("no_progress"));
    assert_eq!(round_stop(&[5, 4, 3, 2, 1]), None);
    assert_eq!(round_stop(&[16, 8, 7, 6, 5, 4, 3, 2]), Some("round_cap"));
    assert_eq!(
        high_med("HIGH a.rs:1 x\n- MED b.rs:2 y\nLOW c\nVERDICT: REQUEST_CHANGES\n"),
        2
    );
}

#[test]
fn a_process_only_review_holds_the_run_without_a_writer_round_or_fix_turn() {
    let mut driver = Fake {
        process_only: true,
        ..fake()
    };
    let record = run(&mut driver, json!({"host":"h"}), "brief");
    assert_eq!(driver.calls, vec![None], "no fix turn");
    assert_eq!(record["exit_code"], 3);
    assert_eq!(record["round_stop"], "process_hold");
    assert_eq!(record["review_rounds"], 0, "the held round is not counted");
    assert!(record["round_counts"].is_null());
    assert_eq!(record["process_hold"]["head"], "final-head");
    assert_eq!(record["process_hold"]["review_run"], "run-review1");
    assert_eq!(driver.pushed, Vec::<String>::new());
}

#[test]
fn a_held_run_continues_review_only_on_the_pinned_head_and_a_fresh_pass_releases_it() {
    let mut driver = Fake {
        process_only: true,
        head_script: vec!["final-head"],
        ..fake()
    };
    let held = run(&mut driver, json!({"host": "h"}), "brief");
    let (reviews, prepares) = (driver.reviews.len(), driver.prepares);
    // The orchestrator repaired the prompt: resume reviews the same head, with no writer turn
    // and without rebasing the head through prepare_review.
    let mut record = held.clone();
    record.as_object_mut().unwrap().remove("exit_code");
    let resumed = run_resume(
        &mut driver,
        record,
        "same-session",
        "ignored by the continuation",
    );
    assert_eq!(
        driver.calls,
        vec![None],
        "no writer turn on the continuation"
    );
    assert_eq!(
        driver.reviews.len(),
        reviews + 1,
        "one fresh review of the same head"
    );
    assert_eq!(driver.prepares, prepares, "the held head is not rebased");
    assert!(
        resumed["process_hold"].is_null(),
        "a passing review releases the hold"
    );
    assert_eq!(resumed["exit_code"], 0);
    assert_eq!(resumed["review"]["verdict"], "ACCEPT");
}

#[test]
fn a_fresh_review_with_findings_keeps_the_hold_until_a_pass() {
    let mut driver = Fake {
        process_only: true,
        head_script: vec!["final-head"],
        ..fake()
    };
    let held = run(&mut driver, json!({"host": "h"}), "brief");
    assert_eq!(held["process_hold"]["head"], "final-head");
    driver.verdicts = vec![("REQUEST_CHANGES", 1)];
    let mut record = held.clone();
    record.as_object_mut().unwrap().remove("exit_code");
    // The fresh review of the held head requests real changes: the writer is resumed (no new
    // commit follows here) and the hold stays on the record, because the head did not pass.
    let resumed = run_resume(&mut driver, record, "same-session", "x");
    assert_eq!(resumed["round_counts"], json!([1]));
    assert_eq!(resumed["process_hold"]["head"], "final-head");
    assert_ne!(resumed["exit_code"], 0);
}

#[test]
fn an_accept_that_still_counts_a_blocker_is_not_pushed() {
    let mut driver = Fake {
        verdicts: vec![("ACCEPT", 1)],
        ..fake()
    };
    let record = run(&mut driver, json!({"host": "h"}), "brief");
    assert_eq!(
        driver.calls.len(),
        2,
        "the counted finding resumes the writer"
    );
    assert_eq!(driver.pushed.len(), 1, "only the later clean ACCEPT pushes");
    assert_eq!(record["round_counts"], json!([1]));
    assert_eq!(record["reviews"][0]["verdict"], "ACCEPT");
}

#[test]
fn a_mixed_review_with_a_real_finding_still_costs_a_round_and_a_fix_turn() {
    let mut driver = Fake {
        process_only: true,
        verdicts: vec![("REQUEST_CHANGES", 1)],
        ..fake()
    };
    let record = run(&mut driver, json!({"host":"h"}), "brief");
    assert_eq!(driver.calls.len(), 2, "the HIGH finding resumes the writer");
    assert_eq!(record["round_counts"], json!([1]));
    assert!(record["process_hold"].is_null());
}
