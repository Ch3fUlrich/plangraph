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

#[test]
fn a_pending_cleanup_the_current_plan_does_not_name_disables_nothing() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), plain("inv-1"));
    let (rows, mut state) = (Rows::default(), PostState::default());
    // A stale checkpoint: another host, another unit, and one that is only named on another host.
    state.cleanup.insert(("alpha".into(), "a.service".into()));
    state
        .cleanup
        .insert(("alpha".into(), "unrelated.timer".into()));
    state.cleanup.insert(("beta".into(), "a.service".into()));
    let before = state.clone();
    let err = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap_err();
    assert!(err.contains("is not a target of this plan"), "{err}");
    assert!(
        host.calls.borrow().is_empty(),
        "no command before every entry is validated: {:?}",
        host.calls.borrow()
    );
    assert_eq!(
        state, before,
        "the stale checkpoint is left for the operator"
    );
}

#[test]
fn a_pending_cleanup_of_the_plans_own_units_is_retried_and_cleared() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), plain("inv-1"));
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    state.cleanup.insert(("alpha".into(), "a.timer".into()));
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    run(&targets, &host, &rows, &mut state).unwrap();
    assert!(state.cleanup.is_empty());
    assert_eq!(host.calls.borrow()[0], "disable a.timer@alpha");
}
