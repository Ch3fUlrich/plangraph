use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, oneshot_ok, run, set, target, Host, Rows};
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

fn units(rows: &Rows) -> Vec<String> {
    rows.0.borrow().iter().map(|r| r.unit.clone()).collect()
}

#[test]
fn a_refused_timer_blocks_only_its_real_dependent_services() {
    // a.service is a oneshot and needs a.timer (refused); c.service has a same-name timer c.timer that was
    // refused too but is no oneshot; b.service has no timer at all.
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    host.runs.insert("b.service".into(), plain("inv-1"));
    host.runs.insert("c.service".into(), plain("inv-1"));
    let (rows, mut state) = (Rows::default(), PostState::default());
    let mut g = gate();
    g.excluded.insert(("a.timer".into(), "alpha".into()));
    g.excluded.insert(("c.timer".into(), "alpha".into()));
    g.oneshot = set(&["a.service"]);
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
        target("c.service", "alpha", &["demo:C"]),
    ];
    let r = apply(&targets, &g, &host, &rows, &mut state, &mut |_| Ok(())).unwrap();
    assert!(r.failed.is_none(), "{:?}", r.failed);
    assert_eq!(r.deployed, ["b.service@alpha", "c.service@alpha"]);
    assert_eq!(r.blocked.len(), 1);
    assert_eq!(r.blocked[0].0, "a.service@alpha");
    assert!(r.blocked[0].1.contains("refused at preflight"));
    assert!(host
        .calls
        .borrow()
        .iter()
        .all(|c| !c.contains("a.service") && !c.contains("a.timer")));
    assert_eq!(units(&rows), ["b.service", "c.service"]);
    assert!(state.failed.is_none());
    assert!(r
        .lines()
        .iter()
        .any(|l| l.starts_with("blocked a.service@alpha")));
}

#[test]
fn a_service_found_to_be_a_oneshot_only_by_its_read_is_blocked_without_stopping_the_step() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    host.runs.insert("b.service".into(), plain("inv-1"));
    let (rows, mut state) = (Rows::default(), PostState::default());
    let mut g = gate();
    g.excluded.insert(("a.timer".into(), "alpha".into()));
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let r = apply(&targets, &g, &host, &rows, &mut state, &mut |_| Ok(())).unwrap();
    assert!(r.failed.is_none());
    assert_eq!(r.deployed, ["b.service@alpha"]);
    assert_eq!(r.blocked.len(), 1);
    assert_eq!(
        units(&rows),
        ["b.service"],
        "no row for the blocked oneshot"
    );
    assert!(state.failed.is_none() && !state.done.contains("a.service@alpha"));
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
