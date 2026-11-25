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
fn a_held_run_continues_review_only_and_a_fresh_review_releases_it() {
    let mut driver = Fake {
        process_only: true,
        head_script: vec!["final-head"],
        ..fake()
    };
    let held = run(&mut driver, json!({"host":"h"}), "brief");
    let reviews = driver.reviews.len();
    // The orchestrator repaired the prompt: resume reviews the same head, with no writer turn.
    let mut record = held.clone();
    record.as_object_mut().unwrap().remove("exit_code");
    let resumed = run_resume(
        &mut driver,
        record,
        "same-session",
        "ignored by a review-only continuation",
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
    assert!(resumed["process_hold"].is_null());
    assert_eq!(resumed["exit_code"], 0);
    assert_eq!(resumed["review"]["verdict"], "ACCEPT");
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
