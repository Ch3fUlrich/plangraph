use super::go_evidence::*;
use super::go_evidence_tests::{
    answer, inputs, receipt, receipt_for_admit, Fake, BASE, HEAD, ROWS, TREE,
};
use super::main_state::State;
use serde_json::{json, Value};

#[test]
fn a_closed_or_retargeted_pull_request_proves_nothing_on_either_read() {
    let (r, main) = (receipt_for_admit(), answer(BASE, State::Green));
    for (label, pr, want) in [
        (
            "closed",
            [("closed", "main"), ("open", "main")],
            "pr_not_open",
        ),
        (
            "retargeted",
            [("open", "next"), ("open", "main")],
            "pr_wrong_target",
        ),
        (
            "closed late",
            [("open", "main"), ("closed", "main")],
            "pr_not_open",
        ),
        (
            "retargeted late",
            [("open", "main"), ("open", "next")],
            "pr_wrong_target",
        ),
    ] {
        let mut gh = Fake::new();
        gh.pr = pr;
        let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap_err();
        assert!(e.starts_with(want), "{label}: {e}");
    }
}

#[test]
fn only_a_run_of_the_requested_pull_request_counts() {
    let (r, main) = (receipt_for_admit(), answer(BASE, State::Green));
    let mut gh = Fake::new();
    // Run 7 is the newest run of the head, but it belongs to pull request 8: run 5 is still the one judged.
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(e.ci.run_id, 5);
    let only_other = json!({"workflow_runs": [{"id": 7, "head_sha": HEAD, "event": "pull_request", "run_attempt": 1,
        "path": ".github/workflows/tests.yml", "pull_requests": [{"number": 8}]}]});
    gh.table.insert(
        format!("actions/workflows/tests.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"),
        only_other,
    );
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap_err();
    assert!(e.contains("no tests.yml pull_request run"), "{e}");
}

#[test]
fn the_judged_attempt_needs_a_ci_full_receipt_for_the_tree_of_the_train() {
    let (r, main) = (receipt_for_admit(), answer(BASE, State::Green));
    let statuses = format!("commits/{HEAD}/statuses?per_page=100");
    for (label, rows) in [
        (
            "another tree",
            vec![receipt(5, 2, &"8".repeat(40), "success")],
        ),
        ("another attempt", vec![receipt(5, 1, TREE, "success")]),
        ("a failed attempt", vec![receipt(5, 2, TREE, "failure")]),
        ("no receipt", vec![]),
    ] {
        let mut gh = Fake::new();
        gh.table.insert(statuses.clone(), Value::Array(rows));
        let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap_err();
        assert!(e.starts_with("ci_unbound"), "{label}: {e}");
    }
    // A receipt written by anyone but the CI publisher is no receipt.
    let mut forged = receipt(5, 2, TREE, "success");
    forged["creator"]["id"] = json!(1);
    let mut gh = Fake::new();
    gh.table.insert(statuses, json!([forged]));
    assert!(collect(&mut gh, &inputs(ROWS, &r, &main))
        .unwrap_err()
        .starts_with("ci_unbound"));
}
