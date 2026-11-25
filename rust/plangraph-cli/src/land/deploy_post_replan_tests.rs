use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, oneshot_ok, run, target, Host, Rows};
use std::collections::BTreeSet;

fn plain(invocation: &str) -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: Some("success".into()),
        exec_main_exit_timestamp: None,
        oneshot: false,
        invocation: Some(invocation.into()),
    }
}

#[test]
fn an_already_active_service_without_a_baseline_invocation_id_is_never_a_restart() {
    let case = |before: RunRead| {
        let mut host = Host::default();
        host.runs.insert("a.service".into(), plain("inv-1"));
        host.before.insert("a.service".into(), before);
        let (rows, mut state) = (Rows::default(), PostState::default());
        let r = run(
            &[target("a.service", "alpha", &["demo:A"])],
            &host,
            &rows,
            &mut state,
        )
        .unwrap();
        let written = rows.0.borrow().len();
        (r.failed.is_none(), written)
    };
    // Running before the install and its id unreadable: the running invocation proves nothing.
    let mut unreadable = plain("x");
    unreadable.invocation = None;
    assert_eq!(case(unreadable), (false, 0));
    // The same running invocation as before.
    assert_eq!(case(plain("inv-1")), (false, 0));
    // Stopped before with no id (never ran): a new invocation is proof.
    let mut stopped = plain("x");
    stopped.invocation = None;
    stopped.is_active = "inactive".into();
    assert_eq!(case(stopped), (true, 1));
    // Running before with another id: restarted.
    assert_eq!(case(plain("inv-0")), (true, 1));
}

#[test]
fn an_unresolved_cleanup_is_persisted_and_blocks_the_retry_until_it_succeeds() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    host.before_fails.insert("a.service".into());
    host.disable_fails.borrow_mut().insert("a.timer".into());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let mut saved: Vec<PostState> = vec![];
    let r = apply(&targets, &gate(), &host, &rows, &mut state, &mut |s| {
        saved.push(s.clone());
        Ok(())
    })
    .unwrap();
    assert!(r.failed.is_some());
    assert_eq!(
        state.cleanup,
        BTreeSet::from([("alpha".to_string(), "a.timer".to_string())])
    );
    assert_eq!(
        saved.last().unwrap().cleanup,
        state.cleanup,
        "the open cleanup was saved"
    );
    // The retry tries the cleanup first, and while it fails nothing else runs.
    let calls_before = host.calls.borrow().len();
    let again = apply(&targets, &gate(), &host, &rows, &mut state, &mut |_| Ok(()));
    assert!(again
        .unwrap_err()
        .contains("could not switch off a.timer@alpha"));
    assert_eq!(
        &host.calls.borrow()[calls_before..],
        ["disable a.timer@alpha"]
    );
    // Once the host lets the timer go off, the cleanup is cleared and the step goes on.
    host.disable_fails.borrow_mut().clear();
    host.before_fails.clear();
    let mut ok_host_state = state.clone();
    let r = apply(
        &targets,
        &gate(),
        &host,
        &rows,
        &mut ok_host_state,
        &mut |_| Ok(()),
    )
    .unwrap();
    assert!(ok_host_state.cleanup.is_empty());
    assert!(r.failed.is_none(), "{:?}", r.failed);
}

#[test]
fn a_blank_baseline_invocation_id_of_an_already_active_service_is_never_a_restart() {
    for blank in ["", "  ", "\t\n"] {
        let mut host = Host::default();
        host.runs.insert("a.service".into(), plain("inv-1"));
        let mut before = plain("x");
        before.invocation = Some(blank.into());
        host.before.insert("a.service".into(), before);
        let (rows, mut state) = (Rows::default(), PostState::default());
        let r = run(
            &[target("a.service", "alpha", &["demo:A"])],
            &host,
            &rows,
            &mut state,
        )
        .unwrap();
        assert!(r.failed.is_some(), "{blank:?} must prove no restart");
        assert!(rows.0.borrow().is_empty());
    }
    // A blank id after the install is no invocation either.
    let mut host = Host::default();
    host.runs.insert("a.service".into(), plain(" "));
    host.started.insert("a.service".into(), " ".into());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r.failed.is_some() && rows.0.borrow().is_empty());
}

/// A state bound to `plan` (the current plan's when `None`, none when `Some("")`) with these pending
/// cleanups, run against `targets`: the error, the host calls and whether the state was left untouched.
fn stale_cleanup(
    plan: Option<&str>,
    pending: &[(&str, &str)],
    targets: &[RunTarget],
) -> (String, usize, bool) {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), plain("inv-1"));
    host.runs.insert("a.timer".into(), active());
    host.runs.insert("b.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState {
        plan: plan.map_or_else(|| plan_id(targets), str::to_owned),
        cleanup: pending
            .iter()
            .map(|(h, u)| (h.to_string(), u.to_string()))
            .collect(),
        ..PostState::default()
    };
    let before = state.clone();
    let err = run(targets, &host, &rows, &mut state).unwrap_err();
    let (calls, same) = (host.calls.borrow().len(), state == before);
    (err, calls, same)
}

#[test]
fn a_pending_cleanup_the_current_plan_does_not_name_disables_nothing() {
    let targets = [target("a.service", "alpha", &["demo:A"])];
    // Another unit, another host, and a unit the plan does not name at all: no command.
    for entry in [
        ("alpha", "unrelated.timer"),
        ("beta", "a.service"),
        ("alpha", "a.timer"),
    ] {
        let (err, calls, same) = stale_cleanup(None, &[entry], &targets);
        assert!(
            err.contains("is not a target of this plan"),
            "{entry:?}: {err}"
        );
        assert_eq!(calls, 0, "{entry:?}");
        assert!(same, "the stale checkpoint is left for the operator");
    }
}

#[test]
fn a_pending_cleanup_needs_the_exact_host_and_unit_pair_of_one_target() {
    // alpha appears in the plan (a.service) and b.timer appears in the plan (on beta), never together.
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.timer", "beta", &["demo:A"]),
    ];
    let (err, calls, same) = stale_cleanup(None, &[("alpha", "b.timer")], &targets);
    assert!(err.contains("is not a target of this plan"), "{err}");
    assert_eq!((calls, same), (0, true));
}

#[test]
fn a_checkpoint_of_another_plan_sharing_a_host_and_timer_issues_no_command() {
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let (err, calls, same) = stale_cleanup(Some("another-plan"), &[("alpha", "a.timer")], &targets);
    assert!(err.contains("belongs to plan another-plan"), "{err}");
    assert_eq!((calls, same), (0, true));
    // A checkpoint with cleanup but no recorded plan has an unknown origin.
    let mut host = Host::default();
    host.runs.insert("a.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    state.cleanup.insert(("alpha".into(), "a.timer".into()));
    let err = run(&targets, &host, &rows, &mut state).unwrap_err();
    assert!(err.contains("names no plan"), "{err}");
    assert!(host.calls.borrow().is_empty());
}

#[test]
fn a_pending_cleanup_of_the_plans_own_units_is_retried_and_cleared() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), plain("inv-1"));
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    state.plan = plan_id(&targets);
    state.cleanup.insert(("alpha".into(), "a.timer".into()));
    run(&targets, &host, &rows, &mut state).unwrap();
    assert!(state.cleanup.is_empty());
    assert_eq!(host.calls.borrow()[0], "disable a.timer@alpha");
}

/// Keeper rows that fail for one unit.
struct FailingRows(&'static str);

impl RowSink for FailingRows {
    fn write(&self, row: &DeployRow) -> Result<(), String> {
        if row.unit == self.0 {
            return Err("keeper unreachable".into());
        }
        Ok(())
    }
}

#[test]
fn a_row_write_failure_switches_off_the_held_timers_and_saves_a_failed_disable() {
    let mut host = Host::default();
    for unit in ["a.service", "b.service"] {
        host.runs.insert(unit.into(), plain("inv-1"));
    }
    host.runs.insert("a.timer".into(), active());
    host.runs.insert("b.timer".into(), active());
    host.disable_fails.borrow_mut().insert("b.timer".into());
    let mut state = PostState::default();
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
        target("b.timer", "alpha", &["demo:B"]),
    ];
    let mut saved: Vec<PostState> = vec![];
    let err = apply(
        &targets,
        &gate(),
        &host,
        &FailingRows("b.service"),
        &mut state,
        &mut |s| {
            saved.push(s.clone());
            Ok(())
        },
    )
    .unwrap_err();
    assert!(err.contains("keeper unreachable"), "{err}");
    // b.timer was installed for its service, whose rows could not be written: it is switched off, and the
    // failed disable is saved so the next run retries it.
    let calls = host.calls.borrow();
    assert!(calls.contains(&"disable b.timer@alpha".to_string()));
    assert!(
        !calls.contains(&"disable a.timer@alpha".to_string()),
        "a.timer was released"
    );
    assert!(state
        .cleanup
        .contains(&("alpha".to_string(), "b.timer".to_string())));
    assert_eq!(saved.last().unwrap().cleanup, state.cleanup);
}
