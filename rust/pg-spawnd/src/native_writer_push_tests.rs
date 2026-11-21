use super::*;

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
