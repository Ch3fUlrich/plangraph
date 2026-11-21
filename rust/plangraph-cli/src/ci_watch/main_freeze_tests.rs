use super::main_freeze::*;
use crate::land::main_state::{Answer, Attempt, Blocking, Failure, Run, State};
use std::collections::BTreeMap;

const T: i64 = 1800;

fn flakes() -> BTreeMap<String, String> {
    BTreeMap::from([("flaky::test".to_owned(), "plangraph:FLAKE-X".to_owned())])
}

fn suite(tests: &[&str]) -> Failure {
    Failure::Suite {
        suite: "rust".into(),
        tests: tests.iter().map(|s| s.to_string()).collect(),
    }
}

fn blocking(run: u64, attempt: u32, failures: Vec<Failure>, stalled: bool) -> Blocking {
    Blocking {
        run,
        event: "push".into(),
        attempt,
        stalled,
        failures,
    }
}

fn answer(state: State, at: i64) -> Answer {
    Answer {
        tip: "tip1".into(),
        state,
        runs: vec![],
        bound_t: T,
        at,
    }
}

fn red(list: Vec<Blocking>, at: i64) -> Answer {
    answer(State::Red { blocking: list }, at)
}

#[derive(Default)]
struct Fake {
    attempts: BTreeMap<u64, (u32, bool)>,
    diagnosis: BTreeMap<u64, Diagnosis>,
    conclusion: Option<String>,
    refuse: bool,
    requests: Vec<(u64, Kind)>,
    fixes: Vec<u64>,
    closed: Vec<String>,
    close_fails: bool,
}

impl Effects for Fake {
    fn attempts(&mut self, run: u64) -> Result<(u32, bool), String> {
        self.attempts
            .get(&run)
            .copied()
            .ok_or_else(|| "no run".into())
    }
    fn diagnose(&mut self, run: u64, _: u32) -> Diagnosis {
        self.diagnosis.get(&run).cloned().unwrap_or_default()
    }
    fn latest_conclusion(&mut self, _: u64) -> Option<String> {
        self.conclusion.clone()
    }
    fn request(&mut self, run: u64, kind: Kind) -> Result<(), String> {
        if self.refuse {
            return Err("HTTP 403".into());
        }
        self.requests.push((run, kind));
        Ok(())
    }
    fn fix_task(&mut self, run: u64, _: &Diagnosis) -> Result<String, String> {
        self.fixes.push(run);
        Ok(format!("plangraph:CI-FIX-{run}"))
    }
    fn close_fix_task(&mut self, key: &str) -> Result<(), String> {
        if self.close_fails {
            return Err("keeper down".into());
        }
        self.closed.push(key.to_owned());
        Ok(())
    }
}

fn flake_diag() -> Diagnosis {
    Diagnosis {
        tests: vec!["flaky::test".into()],
        incomplete: false,
    }
}

fn run_step(state: &mut FreezeState, a: &Answer, fx: &mut Fake) -> Vec<String> {
    step(state, a, &flakes(), fx, false, |_| Ok(())).unwrap()
}

#[test]
fn a_mapped_flake_gets_one_failed_job_rerun_then_the_fix_task() {
    let mut fx = Fake::default();
    fx.diagnosis.insert(5, flake_diag());
    fx.attempts.insert(5, (1, true));
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 1, vec![suite(&["flaky::test"])], false)],
            100,
        ),
        &mut fx,
    );
    assert_eq!(fx.requests, vec![(5, Kind::RerunFailed)]);
    // The new attempt shows; the same flake fails again: no second rerun, the fix task of rule 3.46.
    fx.attempts.insert(5, (2, true));
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 2, vec![suite(&["flaky::test"])], false)],
            200,
        ),
        &mut fx,
    );
    assert_eq!(fx.requests.len(), 1);
    assert_eq!(fx.fixes, vec![5]);
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 2, vec![suite(&["flaky::test"])], false)],
            300,
        ),
        &mut fx,
    );
    assert_eq!(fx.fixes, vec![5], "the fix task is created once per run");
}

#[test]
fn a_failure_without_a_flake_item_goes_to_the_fix_task_without_a_rerun() {
    let mut fx = Fake::default();
    fx.diagnosis.insert(
        5,
        Diagnosis {
            tests: vec!["real::bug".into()],
            incomplete: false,
        },
    );
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 1, vec![suite(&["real::bug"])], false)],
            100,
        ),
        &mut fx,
    );
    assert!(fx.requests.is_empty());
    assert_eq!(fx.fixes, vec![5]);
}

#[test]
fn a_capped_log_never_counts_as_a_known_flake() {
    let mut fx = Fake::default();
    fx.diagnosis.insert(
        5,
        Diagnosis {
            tests: vec!["flaky::test".into()],
            incomplete: true,
        },
    );
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 1, vec![suite(&["flaky::test"])], false)],
            100,
        ),
        &mut fx,
    );
    assert!(fx.requests.is_empty());
    assert_eq!(fx.fixes, vec![5]);
}

#[test]
fn cancelled_or_missing_job_runs_get_one_full_rerun_and_a_second_spends_the_budget() {
    let mut fx = Fake {
        conclusion: Some("cancelled".into()),
        ..Fake::default()
    };
    fx.attempts.insert(6, (1, true));
    let mut state = FreezeState::default();
    let first = red(
        vec![blocking(
            6,
            1,
            vec![Failure::Workflow { attempt: 1 }],
            false,
        )],
        100,
    );
    run_step(&mut state, &first, &mut fx);
    assert_eq!(fx.requests, vec![(6, Kind::RerunAll)]);
    fx.attempts.insert(6, (2, true));
    let second = red(
        vec![blocking(
            6,
            2,
            vec![Failure::Workflow { attempt: 2 }],
            false,
        )],
        200,
    );
    let out = run_step(&mut state, &second, &mut fx);
    assert_eq!(fx.requests.len(), 1, "no third action");
    assert!(out.iter().any(|l| l.starts_with("budget spent")));
    let alert = state
        .alerts
        .values()
        .find(|a| a.kind == "recovery_budget_spent")
        .unwrap();
    assert_eq!(alert.due, "routing");
    assert!(alert.detail.contains("run 6 attempt 2"));
}

#[test]
fn a_hung_attempt_is_cancelled_then_rerun_once_and_a_second_hang_spends_the_budget() {
    let mut fx = Fake::default();
    fx.attempts.insert(7, (1, false));
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 1, vec![Failure::Workflow { attempt: 1 }], true)],
            100,
        ),
        &mut fx,
    );
    assert_eq!(fx.requests, vec![(7, Kind::Cancel)]);
    // The cancel is not reconciled yet: nothing else is requested.
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 1, vec![Failure::Workflow { attempt: 1 }], true)],
            160,
        ),
        &mut fx,
    );
    assert_eq!(fx.requests.len(), 1);
    // The attempt is terminal now: the one rerun follows.
    fx.attempts.insert(7, (1, true));
    fx.conclusion = Some("cancelled".into());
    run_step(
        &mut state,
        &red(
            vec![blocking(
                7,
                1,
                vec![Failure::Workflow { attempt: 1 }],
                false,
            )],
            200,
        ),
        &mut fx,
    );
    assert_eq!(fx.requests, vec![(7, Kind::Cancel), (7, Kind::RerunAll)]);
    // The rerun shows and hangs again.
    fx.attempts.insert(7, (2, false));
    let mut out = run_step(
        &mut state,
        &red(
            vec![blocking(7, 2, vec![Failure::Workflow { attempt: 1 }], true)],
            300,
        ),
        &mut fx,
    );
    out.extend(run_step(
        &mut state,
        &red(
            vec![blocking(7, 2, vec![Failure::Workflow { attempt: 1 }], true)],
            400,
        ),
        &mut fx,
    ));
    assert_eq!(fx.requests.len(), 2);
    assert!(out.iter().any(|l| l.starts_with("budget spent")));
}

#[test]
fn a_request_github_refuses_stays_red_and_alerts_routing_with_the_ref_released() {
    let mut fx = Fake {
        refuse: true,
        ..Fake::default()
    };
    fx.diagnosis.insert(5, flake_diag());
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(5, 1, vec![suite(&["flaky::test"])], false)],
            100,
        ),
        &mut fx,
    );
    assert!(state.pending.is_none());
    assert_eq!(
        state
            .alerts
            .values()
            .filter(|a| a.kind == "recovery_refused" && a.due == "routing")
            .count(),
        1
    );
}

#[test]
fn a_lost_response_waits_ten_minutes_then_is_unresolved_alerted_and_the_next_run_is_recovered() {
    let mut fx = Fake::default();
    fx.diagnosis.insert(5, flake_diag());
    fx.diagnosis.insert(9, flake_diag());
    fx.attempts.insert(5, (1, true));
    fx.attempts.insert(9, (1, true));
    let mut state = FreezeState::default();
    let both = |at| {
        red(
            vec![
                blocking(5, 1, vec![suite(&["flaky::test"])], false),
                blocking(9, 1, vec![suite(&["flaky::test"])], false),
            ],
            at,
        )
    };
    run_step(&mut state, &both(100), &mut fx);
    assert_eq!(fx.requests, vec![(5, Kind::RerunFailed)]);
    // One action per ref at a time: the second run waits while the first is unreconciled.
    run_step(&mut state, &both(400), &mut fx);
    assert_eq!(fx.requests.len(), 1);
    // The attempts show nothing new after 10 minutes: unresolved, alert, released; run 5 is never retried.
    run_step(&mut state, &both(100 + RECONCILE_SECS), &mut fx);
    assert_eq!(state.unresolved.len(), 1);
    assert!(state
        .alerts
        .values()
        .any(|a| a.kind == "recovery_unresolved" && a.due == "routing"));
    assert_eq!(
        fx.requests,
        vec![(5, Kind::RerunFailed), (9, Kind::RerunFailed)]
    );
}

#[test]
fn a_run_id_none_alerts_and_alerts_are_raised_once() {
    let mut fx = Fake::default();
    let mut state = FreezeState::default();
    let a = red(vec![blocking(0, 0, vec![], false)], 100);
    run_step(&mut state, &a, &mut fx);
    run_step(&mut state, &a, &mut fx);
    assert_eq!(state.alerts.len(), 1);
    assert!(fx.requests.is_empty());
}

fn attempt(status: &str, started: i64) -> Attempt {
    Attempt {
        number: 1,
        status: status.into(),
        conclusion: None,
        started,
        concluded: 0,
        tier: "hosted".into(),
        jobs: vec![],
    }
}

#[test]
fn a_red_main_without_progress_alerts_after_thirty_minutes_and_a_young_attempt_is_progress() {
    let mut state = FreezeState::default();
    let mut fx = Fake::default();
    let list = vec![blocking(5, 1, vec![suite(&["real::bug"])], false)];
    fx.diagnosis.insert(
        5,
        Diagnosis {
            tests: vec!["real::bug".into()],
            incomplete: false,
        },
    );
    run_step(&mut state, &red(list.clone(), 0), &mut fx);
    run_step(
        &mut state,
        &red(list.clone(), NO_PROGRESS_SECS - 1),
        &mut fx,
    );
    assert!(!state
        .alerts
        .values()
        .any(|a| a.kind == "main_red_no_progress"));
    run_step(&mut state, &red(list.clone(), NO_PROGRESS_SECS), &mut fx);
    assert!(state
        .alerts
        .values()
        .any(|a| a.kind == "main_red_no_progress" && a.due == "routing"));

    let mut state = FreezeState::default();
    let mut moving = red(list.clone(), 0);
    moving.runs = vec![Run {
        id: 5,
        event: "push".into(),
        attempts: vec![attempt("in_progress", 0)],
        refname: "main".into(),
        fast: false,
    }];
    refresh_alerts(&mut state, &moving, false);
    let mut later = red(list, NO_PROGRESS_SECS + 10);
    later.runs = vec![Run {
        id: 5,
        event: "push".into(),
        attempts: vec![attempt("in_progress", NO_PROGRESS_SECS)],
        refname: "main".into(),
        fast: false,
    }];
    refresh_alerts(&mut state, &later, false);
    assert!(state.alerts.is_empty());
}

#[test]
fn pending_unknown_and_unprotected_alerts_name_the_party_that_is_due() {
    let mut state = FreezeState::default();
    refresh_alerts(&mut state, &answer(State::Pending, 0), false);
    refresh_alerts(
        &mut state,
        &answer(State::Pending, T + PENDING_EXTRA_SECS),
        false,
    );
    assert!(state.alerts.is_empty());
    refresh_alerts(
        &mut state,
        &answer(State::Pending, T + PENDING_EXTRA_SECS + 1),
        false,
    );
    assert_eq!(
        state.alerts.values().next().unwrap().kind,
        "main_pending_aged"
    );

    let mut state = FreezeState::default();
    let unknown = |at| {
        answer(
            State::StateUnknown {
                failing_read: "list runs: HTTP 502".into(),
            },
            at,
        )
    };
    refresh_alerts(&mut state, &unknown(0), false);
    refresh_alerts(&mut state, &unknown(UNKNOWN_SECS), false);
    let alert = state.alerts.values().next().unwrap();
    assert_eq!(
        (alert.kind.as_str(), alert.due.as_str()),
        ("main_state_unknown", "host session")
    );
    assert!(alert.detail.contains("HTTP 502"));
    refresh_alerts(&mut state, &answer(State::Green, UNKNOWN_SECS + 5), false);
    assert!(state.alerts.is_empty(), "green clears the open alerts");

    refresh_alerts(&mut state, &answer(State::RefNotForwardOnly, 1), false);
    let alert = state.alerts.values().next().unwrap();
    assert_eq!(
        (alert.kind.as_str(), alert.due.as_str()),
        ("main_ref_not_forward_only", "operator")
    );
}

#[test]
fn a_red_rerun_after_the_train_pr_closed_creates_the_fix_task_and_a_green_rerun_closes_it() {
    let mut fx = Fake::default();
    fx.diagnosis.insert(
        7,
        Diagnosis {
            tests: vec!["real::bug".into()],
            incomplete: false,
        },
    );
    let mut state = FreezeState::default();
    // The train pull request is long closed; the rerun of main's full run (run 7) ended red.
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 2, vec![suite(&["real::bug"])], false)],
            100,
        ),
        &mut fx,
    );
    assert_eq!(fx.fixes, vec![7]);
    assert_eq!(state.fix_tasks.get("plangraph:CI-FIX-7"), Some(&7));
    // Still red: no second task. Then a green rerun: the task closes, main's sha did not change.
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 2, vec![suite(&["real::bug"])], false)],
            200,
        ),
        &mut fx,
    );
    assert_eq!(fx.fixes.len(), 1);
    run_step(&mut state, &answer(State::Green, 300), &mut fx);
    assert_eq!(fx.closed, vec!["plangraph:CI-FIX-7".to_owned()]);
    assert!(state.fix_tasks.is_empty());
    // A later red of the same run creates a fresh task.
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 3, vec![suite(&["real::bug"])], false)],
            400,
        ),
        &mut fx,
    );
    assert_eq!(fx.fixes, vec![7, 7]);
}

#[test]
fn a_close_that_fails_stays_open_and_is_retried_on_the_next_pass() {
    let mut fx = Fake {
        close_fails: true,
        ..Fake::default()
    };
    fx.diagnosis.insert(
        7,
        Diagnosis {
            tests: vec!["real::bug".into()],
            incomplete: false,
        },
    );
    let mut state = FreezeState::default();
    run_step(
        &mut state,
        &red(
            vec![blocking(7, 1, vec![suite(&["real::bug"])], false)],
            100,
        ),
        &mut fx,
    );
    let out = run_step(&mut state, &answer(State::Green, 200), &mut fx);
    assert!(out.iter().any(|l| l.starts_with("close pending")));
    assert_eq!(state.fix_tasks.len(), 1);
    fx.close_fails = false;
    run_step(&mut state, &answer(State::Green, 300), &mut fx);
    assert!(state.fix_tasks.is_empty());
    assert_eq!(fx.closed.len(), 1);
}
