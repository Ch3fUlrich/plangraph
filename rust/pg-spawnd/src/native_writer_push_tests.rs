use super::*;

#[test]
fn push_gate_fix_turn_without_new_commit_keeps_the_refusal() {
    let mut driver = Fake {
        push_failures: 1,
        head_script: vec!["base", "final-head"],
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(record["status"], "failed", "{record}");
    assert_eq!(driver.pushed, ["final-head"]);
    assert_eq!(driver.reviews.len(), 1);
    assert_eq!(record["resumes"], 1);
    let reason = record["reason"].as_str().unwrap();
    assert!(reason.contains("no_commit"));
    assert!(reason.contains("pre-push: fixture scrub hit"));
}

#[test]
fn push_gate_fix_turn_reviews_and_pushes_the_new_commit() {
    let mut driver = Fake {
        push_failures: 1,
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(record["status"], "completed", "{record}");
    assert_eq!(driver.calls, [None, Some("same-session".into())]);
    assert!(driver.prompts[1].contains("pre-push: fixture scrub hit"));
    assert_eq!(driver.pushed, ["final-head", "final-head-2"]);
    assert_eq!(driver.stamped, driver.pushed);
    assert_eq!(record["review"]["head"], "final-head-2");
    assert_eq!(record["pushed"], "refs/heads/lane/fixture");
    assert_eq!(
        record["push_failures"][0]["output"],
        "pre-push: fixture scrub hit"
    );
}

#[test]
fn push_gate_fix_turn_stops_with_gate_output_when_the_budget_is_exhausted() {
    let mut driver = Fake {
        push_failures: 99,
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(driver.pushed.len(), 3);
    assert_eq!(record["status"], "failed");
    assert_eq!(record["exit_code"], 7);
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .contains("pre-push: fixture scrub hit"));
    assert_eq!(record["push_failures"].as_array().unwrap().len(), 3);
    assert_eq!(record["final_head"], "final-head-3");
    assert!(record["pushed"].is_null());
}

#[test]
fn host_blocked_reclaim_keeps_the_accepted_head_and_retry_only_pushes() {
    let mut driver = Fake {
        reclaim_failures: 1,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["status"], "failed", "{record}");
    assert_eq!(record["exit_code"], 7);
    let reason = record["reason"].as_str().unwrap();
    assert!(reason.contains("push_blocked_by_host") && reason.contains("pid 4242"));
    assert_eq!(record["push_pending"]["head"], "final-head");
    // The writer is not asked to fix a host cause, and nothing was pushed.
    assert_eq!(driver.calls, [None]);
    assert!(driver.pushed.is_empty());
    assert!(record["push_failures"].is_null());
    let reviews = driver.reviews.len();

    // The retry runs the same record: no writer turn, no review, one push of the same head.
    let retried = run(&mut driver, record, "brief");
    assert_eq!(retried["status"], "completed", "{retried}");
    assert_eq!(retried["pushed"], "refs/heads/lane/fixture");
    assert!(retried["push_pending"].is_null());
    assert_eq!(driver.calls, [None]);
    assert_eq!(driver.reviews.len(), reviews);
    assert_eq!(driver.pushed, ["final-head"]);
}

#[test]
fn push_only_retry_refuses_a_head_that_moved() {
    let mut driver = Fake {
        head_script: vec!["moved-head"],
        ..fake()
    };
    let record = run(
        &mut driver,
        json!({"push_pending":{"head":"final-head","receipt":"r"}}),
        "brief",
    );
    assert_eq!(record["status"], "failed");
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .contains("no longer the accepted head"));
    assert!(driver.calls.is_empty() && driver.pushed.is_empty());
}

#[test]
fn resume_of_an_ended_run_with_a_pending_push_only_pushes() {
    // `pg-spawnd resume` enters through `run_resume`, never `run`.
    let mut driver = Fake {
        reclaim_failures: 1,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["push_pending"]["head"], "final-head");
    let reviews = driver.reviews.len();

    let retried = run_resume(&mut driver, record, "same-session", "operator retry");
    assert_eq!(retried["status"], "completed", "{retried}");
    assert_eq!(retried["pushed"], "refs/heads/lane/fixture");
    assert!(retried["push_pending"].is_null());
    // No writer turn (the operator prompt never reaches the writer) and no new review.
    assert_eq!(driver.calls, [None]);
    assert!(driver
        .prompts
        .iter()
        .all(|prompt| !prompt.contains("operator retry")));
    assert_eq!(driver.reviews.len(), reviews);
    assert_eq!(driver.pushed, ["final-head"]);
}

#[test]
fn a_preflight_cap_refusal_keeps_the_accepted_head_for_a_push_only_retry() {
    let refused = std::rc::Rc::new(std::cell::Cell::new(true));
    let flag = refused.clone();
    let mut driver = Fake {
        host_push: Some(Box::new(move |_| {
            if flag.get() {
                Err("pre-push: cargo_target_cap: measured 4096 bytes in the shared target, cap 1024 bytes".into())
            } else {
                Ok(Some("refs/heads/lane/fixture".into()))
            }
        })),
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["status"], "failed", "{record}");
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .contains("push_blocked_by_host"));
    assert_eq!(record["push_pending"]["head"], "final-head");
    // The writer is not asked to fix a host cause.
    assert_eq!(driver.calls, [None]);
    let reviews = driver.reviews.len();

    refused.set(false);
    let retried = run_resume(&mut driver, record, "same-session", "operator retry");
    assert_eq!(retried["status"], "completed", "{retried}");
    assert_eq!(retried["pushed"], "refs/heads/lane/fixture");
    assert_eq!(driver.calls, [None]);
    assert_eq!(driver.reviews.len(), reviews);
    assert_eq!(driver.pushed, ["final-head", "final-head"]);
}
