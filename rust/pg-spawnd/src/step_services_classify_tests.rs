use crate::step_services::*;
use pg_worker_core::batch_steps::Classification;

fn run(id: u64, status: &str, conclusion: Option<&str>) -> CiRun {
    CiRun {
        name: "CI".into(),
        event: "pull_request".into(),
        id,
        attempt: 1,
        status: status.into(),
        conclusion: conclusion.map(str::to_owned),
    }
}

fn on(event: &str, run: CiRun) -> CiRun {
    CiRun {
        event: event.into(),
        ..run
    }
}

#[test]
fn a_head_without_runs_is_never_green() {
    assert!(matches!(classify(&[], &[], false), Reading::Pending(_)));
}

#[test]
fn a_cancelled_run_is_red() {
    let Reading::Done(class) = classify(&[run(3, "completed", Some("cancelled"))], &[], false)
    else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
    assert_eq!(class.run, 3);
}

#[test]
fn red_wins_over_a_run_still_going() {
    let Reading::Done(class) = classify(
        &[
            run(3, "completed", Some("failure")),
            CiRun {
                name: "lint".into(),
                ..run(4, "in_progress", None)
            },
        ],
        &[],
        false,
    ) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
}

#[test]
fn green_needs_every_required_workflow_to_have_run() {
    let only_lint = CiRun {
        name: "lint".into(),
        ..run(4, "completed", Some("success"))
    };
    let required = ["CI".to_owned()];
    assert!(matches!(
        classify(std::slice::from_ref(&only_lint), &required, false),
        Reading::Pending(_)
    ));
    let ci = run(5, "completed", Some("success"));
    let Reading::Done(class) = classify(&[only_lint, ci], &required, false) else {
        panic!("pending");
    };
    assert_eq!(class.run, 5, "the run named is the required workflow's");
}

#[test]
fn an_older_failed_run_of_a_workflow_does_not_make_the_head_red() {
    let old_red = run(3, "completed", Some("failure"));
    let new_green = run(7, "completed", Some("success"));
    let Reading::Done(class) = classify(&[old_red, new_green], &["CI".to_owned()], false) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Green));
    assert_eq!(
        (class.run, class.attempt),
        (7, 1),
        "the newer run is the receipt"
    );
}

#[test]
fn a_newer_failure_beats_an_older_pass() {
    let Reading::Done(class) = classify(
        &[
            run(3, "completed", Some("success")),
            run(7, "completed", Some("failure")),
        ],
        &["CI".to_owned()],
        false,
    ) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
    assert_eq!(class.run, 7);
}

#[test]
fn a_draft_lane_part_passes_on_its_skipped_required_workflow() {
    // The policy of .github/workflows/tests.yml: a draft lane/* pull request runs no job and a
    // skipped required check reports success; its admission is review and the fast gates.
    let skipped = [run(5, "completed", Some("skipped"))];
    let required = ["CI".to_owned()];
    assert!(matches!(
        classify(&skipped, &required, false),
        Reading::Unproven(_)
    ));
    let Reading::Done(class) = classify(&skipped, &required, true) else {
        panic!("not decided");
    };
    assert!(matches!(class.classification, Classification::Green));
    assert_eq!(class.run, 5);
    // The policy never excuses a failure.
    let Reading::Done(red) = classify(&[run(6, "completed", Some("failure"))], &required, true)
    else {
        panic!("pending");
    };
    assert!(matches!(red.classification, Classification::Red));
}

#[test]
fn a_failure_of_another_event_is_not_hidden_by_a_newer_pass() {
    // The CI watcher selects per head and event; a dispatch run that failed is still the state
    // of that event after a newer pull_request run passed.
    let runs = [
        on("workflow_dispatch", run(4, "completed", Some("failure"))),
        on("pull_request", run(9, "completed", Some("success"))),
    ];
    let Reading::Done(class) = classify(&runs, &["CI".to_owned()], false) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
    assert_eq!(class.run, 4);
}

#[test]
fn the_part_policy_excuses_only_a_skipped_pull_request_run() {
    let required = ["CI".to_owned()];
    let dispatched = [on(
        "workflow_dispatch",
        run(5, "completed", Some("skipped")),
    )];
    assert!(matches!(
        classify(&dispatched, &required, true),
        Reading::Unproven(_)
    ));
}
