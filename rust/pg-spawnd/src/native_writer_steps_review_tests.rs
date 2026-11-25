use crate::native_writer_steps_fake::*;
use pg_spawnd::step_runner::*;
use serde_json::json;

#[test]
fn the_previous_review_survives_a_restart_for_the_delta_review() {
    let dir = tempfile::tempdir().unwrap();
    let head = sha('1');
    let mut first = agents(Fake::default(), dir.path());
    changes(&mut first, &head, 1);
    // A new process: the adapter and the driver are new, the findings directory is the same.
    let mut second = agents(Fake::default(), dir.path());
    second.driver.stamped.push(sha('2'));
    let accepted = second.review(&sha('2'), "stamp").unwrap();
    commit(&second.steps, &sha('2'), &accepted);
    let previous = second.driver.reviews[0].1.as_ref().expect("a delta review");
    assert_eq!(previous["head"], head.as_str());
    assert!(previous["text"].as_str().unwrap().contains("HIGH a.rs:1"));
    // An accepting review clears it: the next head is not a delta of a closed round.
    second.driver.stamped.push(sha('3'));
    second.review(&sha('3'), "stamp").unwrap();
    assert!(second.driver.reviews[1].1.is_none());
}

#[test]
fn rounds_that_do_not_improve_stop_the_batch_before_the_fix_turn() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let mut last = None;
    for round in 1..=3 {
        let head = sha(char::from(b'a' + round));
        let review = changes(&mut steps, &head, 2);
        last = Some((head, review.review));
        if round < 3 {
            let (head, review) = last.as_ref().unwrap();
            steps.fix(head, "writer-session", review).unwrap();
        }
    }
    let (head, review) = last.unwrap();
    let turns = steps.driver.turns.len();
    let error = steps.fix(&head, "writer-session", &review).unwrap_err();
    assert_eq!(error, "round_stop: no_progress");
    assert_eq!(steps.driver.turns.len(), turns, "the stop spent no turn");
}

#[test]
fn the_eighth_round_is_the_last_even_when_every_round_improves() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    // Counts 128,64,..,1 halve every round, so only the cap ends it, at round 8.
    let mut review = None;
    for round in 1..=8u8 {
        let head = sha(char::from(b'a' + round));
        review = Some((
            head.clone(),
            changes(&mut steps, &head, 1usize << (8 - round)).review,
        ));
    }
    let (head, id) = review.unwrap();
    assert_eq!(
        steps.fix(&head, "s", &id).unwrap_err(),
        "round_stop: round_cap"
    );
}

#[test]
fn a_review_killed_before_its_row_does_not_count() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let first = changes(&mut steps, &sha('b'), 2);
    // A second review ran and was kept, but the process died before the runner saved its row.
    steps.driver.stamped.push(sha('c'));
    steps.driver.verdicts = vec!["REQUEST_CHANGES"];
    steps.review(&sha('c'), "stamp").unwrap();
    // After the restart the reviews on record are the first alone: it is the delta base, and
    // the stop rule sees one round, not two.
    let mut again = agents(Fake::default(), dir.path());
    again.driver.stamped.push(sha('d'));
    again.review(&sha('d'), "stamp").unwrap();
    assert_eq!(
        again.driver.reviews[0].1.as_ref().unwrap()["head"],
        sha('b').as_str()
    );
    assert!(again.fix(&sha('b'), "s", &first.review).is_ok());
}

#[test]
fn a_tree_that_differs_from_the_commit_gets_no_gates_and_no_stamp() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = stamping(
        dir.path(),
        Fake {
            dirty: true,
            ..Fake::default()
        },
    );
    let error = steps.fast_checks(&sha('1')).unwrap_err();
    assert!(error.contains("differs from the committed tree"), "{error}");
    assert!(steps.driver.stamped.is_empty(), "the gates did not run");
    assert!(!dir.path().join(sha('1')).exists());
}

#[test]
fn a_main_that_moved_is_refused_before_any_gate_runs() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = stamping(
        dir.path(),
        Fake {
            probe: true,
            main_moved: true,
            ..Fake::default()
        },
    );
    let error = steps.fast_checks(&sha('1')).unwrap_err();
    assert!(error.contains("moved"), "{error}");
    assert!(steps.driver.stamped.is_empty());
    assert!(
        steps.driver.saved.is_empty(),
        "nothing was published for the head"
    );
}

#[test]
fn the_review_admission_fields_are_published_before_the_review() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = stamping(
        dir.path(),
        Fake {
            probe: true,
            record: json!({"run_id": "run-x", "status": "running", "base_sha": sha('9')}),
            ..Fake::default()
        },
    );
    let head = sha('1');
    steps.fast_checks(&head).unwrap();
    let saved = &steps.driver.saved[0];
    assert_eq!(saved["base_admitted"], true);
    assert_eq!(saved["base_sha"], BASE, "the base the review basis names");
    assert_eq!(saved["final_head"], head.as_str());
    assert_eq!(
        saved["fast_checks"],
        json!({"head": head, "status": "passed"})
    );
    assert_eq!(saved["review_base"]["mode"], "current");
    assert_eq!(saved["review_base"]["head"], head.as_str());
    assert_eq!(saved["run_id"], "run-x", "the rest of the record is kept");
    assert!(
        saved.get("review").is_none(),
        "no round yet, so no delta base"
    );

    // After a round that asked for changes the next head's record carries it as the delta base.
    changes(&mut steps, &head, 1);
    let next = sha('2');
    steps.fast_checks(&next).unwrap();
    assert_eq!(
        steps.driver.saved[1]["review"]["verdict"],
        "REQUEST_CHANGES"
    );
    assert_eq!(steps.driver.saved[1]["review"]["head"], head.as_str());
}

#[test]
fn the_push_may_publish_only_a_recorded_accepting_review_of_the_exact_head() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let head = sha('1');
    steps.driver.stamped.push(head.clone());
    let accepted = steps.review(&head, "stamp").unwrap();
    // Kept but not recorded (a kill before the row): no receipt.
    assert!(steps.verify_review(&head, &accepted.review).is_err());
    commit(&steps.steps, &head, &accepted);
    steps.verify_review(&head, &accepted.review).unwrap();
    // The reference of a resumed row can name any run or any head.
    assert!(steps.verify_review(&head, "run-other").is_err());
    assert!(steps.verify_review(&sha('2'), &accepted.review).is_err());
}

#[test]
fn a_request_for_changes_is_no_receipt_for_a_push() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let head = sha('1');
    let changes = changes(&mut steps, &head, 1);
    let error = steps.verify_review(&head, &changes.review).unwrap_err();
    assert!(error.contains("no recorded accepting review"), "{error}");
}

#[test]
fn a_row_that_says_accept_over_a_kept_request_for_changes_authorizes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let head = sha('1');
    let rejected = changes(&mut steps, &head, 1);
    // The steps file is rewritten so the row reads ACCEPT; the kept review run still says
    // REQUEST_CHANGES, and the kept run is the receipt.
    let forged = pg_worker_core::batch_steps::Review {
        verdict: pg_worker_core::batch_steps::Verdict::Accept,
        review: rejected.review.clone(),
    };
    std::fs::remove_file(&steps.steps).unwrap();
    commit(&steps.steps, &head, &forged);
    let error = steps.verify_review(&head, &rejected.review).unwrap_err();
    assert!(error.contains("no recorded accepting review"), "{error}");
}
