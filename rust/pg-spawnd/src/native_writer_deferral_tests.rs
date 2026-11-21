use super::*;

#[test]
fn native_defers_when_the_host_offers_no_repository_review_seat() {
    let mut driver = Fake {
        defer_review_on: Some(1),
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0);
    assert_eq!(record["status"], "completed");
    assert_eq!(record["review_status"], "deferred_to_batch_seat");
    assert_eq!(record["final_head"], "final-head");
    assert_eq!(
        record["review_causes"][0]["reason"],
        "repository_review_tools_unavailable"
    );
    assert_eq!(record["review_causes"][1]["reason"], "limited");
    assert_eq!(record["review_rounds"], 0);
    assert_eq!(driver.reviews.len(), 1);
    assert!(driver.pushed.is_empty());
}

#[test]
fn native_defers_when_no_repository_seat_remains_after_requested_changes() {
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 1)],
        defer_review_on: Some(2),
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0);
    assert_eq!(record["status"], "completed");
    assert_eq!(record["review_status"], "deferred_to_batch_seat");
    assert_eq!(record["final_head"], "final-head-2");
    assert_eq!(record["review_rounds"], 1);
    assert_eq!(record["review"]["verdict"], "REQUEST_CHANGES");
    assert_eq!(driver.reviews.len(), 2);
    assert!(driver.pushed.is_empty());
}

#[test]
fn native_defers_on_a_retry_after_a_review_error() {
    let mut driver = Fake {
        fail_first_review: true,
        defer_review_on: Some(2),
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["status"], "completed");
    assert_eq!(record["review_status"], "deferred_to_batch_seat");
    assert_eq!(record["review_rounds"], 0);
    assert_eq!(driver.reviews.len(), 2);
    assert!(driver.pushed.is_empty());
}
