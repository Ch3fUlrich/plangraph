use super::deploy_post::*;
use super::deploy_post_tests::{
    active, disables, gate, go, host_of, oneshot_ok, run, target, with_result, Host, Rows, REV, T0,
};
use std::collections::BTreeSet;

fn plain(invocation: &str) -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: Some("success".into()),
        invocation: Some(invocation.into()),
        ..active()
    }
}

fn svc(name: &str, run: RunRead) -> Host {
    host_of(&[(name, run), ("a.timer", active())])
}

#[test]
fn a_failed_service_switches_off_only_a_timer_the_go_names_and_this_attempt_installed() {
    let bad = with_result(oneshot_ok(), "exit-code");
    let host = svc("a.service", bad.clone());
    let (r, _, state) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
    assert!(
        r.failed.is_some() && disables(&host).is_empty(),
        "the timer is not in the GO"
    );
    assert_eq!(state.failed.as_deref(), Some("a.service@alpha"));
    let host = svc("a.service", bad);
    go(
        &host,
        &[
            target("a.service", "alpha", &["demo:A"]),
            target("a.timer", "alpha", &["demo:A"]),
        ],
    );
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
}

#[test]
fn a_failed_baseline_read_changes_nothing_but_still_cleans_up_the_attempts_own_timer() {
    let mut host = svc("a.service", oneshot_ok());
    host.before_fails.insert("a.service".into());
    let (r, rows, state) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
    assert!(r.failed.unwrap().1.contains("read before install") && rows.is_empty());
    assert_eq!(*host.calls.borrow(), ["read a.service@alpha"]);
    assert!(
        state.failed.is_none(),
        "nothing changed, so the rerun stays open"
    );
    let mut host = svc("a.service", oneshot_ok());
    host.before_fails.insert("a.service".into());
    let both = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let (_, _, state) = go(&host, &both);
    assert!(!host
        .calls
        .borrow()
        .contains(&"install a.service@alpha".to_string()));
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
    assert!(state.failed.is_none() && state.done.is_empty());
}

#[test]
fn a_service_that_is_no_oneshot_needs_a_fresh_invocation_and_a_successful_start() {
    let case = |run_read: RunRead, started: Option<&str>, before: Option<RunRead>| {
        let mut host = host_of(&[("a.service", run_read)]);
        match started {
            Some(s) => host
                .started
                .insert("a.service".into(), s.into())
                .map(|_| ()),
            None => host.silent_install.insert("a.service".into()).then_some(()),
        };
        if let Some(b) = before {
            host.before.insert("a.service".into(), b);
        }
        let (r, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
        (r.failed.is_none(), rows.len())
    };
    assert_eq!(
        case(plain("inv-2"), Some("inv-2"), Some(plain("inv-1"))),
        (true, 1)
    );
    // Nothing restarted it, a restart by something else, no invocation reported, a start that did not succeed.
    assert_eq!(
        case(plain("inv-1"), Some("inv-1"), Some(plain("inv-1"))),
        (false, 0)
    );
    assert_eq!(case(plain("inv-3"), Some("inv-2"), None), (false, 0));
    assert_eq!(case(plain("inv-2"), None, None), (false, 0));
    assert_eq!(
        case(
            with_result(plain("inv-2"), "start-limit-hit"),
            Some("inv-2"),
            None
        ),
        (false, 0)
    );
}

#[test]
fn an_already_active_service_without_a_usable_baseline_id_is_never_a_restart() {
    let case = |before_id: Option<&str>, before_state: &str| {
        let mut before = plain("x");
        before.invocation = before_id.map(str::to_owned);
        before.is_active = before_state.into();
        let mut host = host_of(&[("a.service", plain("inv-1"))]);
        host.before.insert("a.service".into(), before);
        let (r, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
        (r.failed.is_none(), rows.len())
    };
    for blank in [None, Some(""), Some("  "), Some("\t\n")] {
        assert_eq!(case(blank, "active"), (false, 0), "{blank:?}");
    }
    assert_eq!(case(Some("inv-1"), "active"), (false, 0));
    assert_eq!(
        case(None, "inactive"),
        (true, 1),
        "stopped without an id: a new invocation is proof"
    );
    assert_eq!(case(Some("inv-0"), "active"), (true, 1));
    // A blank id after the install is no invocation either.
    let mut host = host_of(&[("a.service", plain(" "))]);
    host.started.insert("a.service".into(), " ".into());
    let (r, rows, _) = go(&host, &[target("a.service", "alpha", &["demo:A"])]);
    assert!(r.failed.is_some() && rows.is_empty());
}

#[test]
fn a_binary_needs_installed_and_the_documented_read_json_is_judged_good() {
    let ok = RunRead {
        is_active: "installed".into(),
        ..active()
    };
    let (g, base) = (gate(), Baseline::default());
    assert!(
        ok.good("bin:tool", &g, &base, T0, None) && !ok.good("a.service", &g, &base, T0, Some("i"))
    );
    let json = |text: &str| serde_json::from_str::<RunRead>(text).unwrap();
    let oneshot = json(
        r#"{"is_active":"inactive","oneshot":true,"result":"success","exec_main_exit_timestamp":"2026-11-24T01:00:00Z","invocation":"inv-1"}"#,
    );
    let svc = json(r#"{"is_active":"active","result":"success","invocation":"inv-1"}"#);
    assert!(oneshot.good("a.service", &g, &base, T0, Some("inv-1")));
    assert!(svc.good("a.service", &g, &base, T0, Some("inv-1")));
    assert!(json(r#"{"is_active":"installed"}"#).good("bin:tool", &g, &base, T0, None));
}

/// A state bound to `plan` (the current plan on `REV` when `None`) with these pending cleanups, run against
/// `targets`: the error, the host calls and whether the state was left untouched.
fn stale(
    plan: Option<&str>,
    pending: &[(&str, &str)],
    targets: &[RunTarget],
) -> (String, usize, bool) {
    let host = host_of(&[
        ("a.service", plain("inv-1")),
        ("a.timer", active()),
        ("b.timer", active()),
    ]);
    let mut state = PostState {
        plan: plan.map_or_else(|| plan_id(targets, REV), str::to_owned),
        cleanup: pending
            .iter()
            .map(|(h, u)| (h.to_string(), u.to_string()))
            .collect(),
        ..PostState::default()
    };
    let before = state.clone();
    let err = run(targets, &host, &Rows::default(), &mut state).unwrap_err();
    let (calls, same) = (host.calls.borrow().len(), state == before);
    (err, calls, same)
}

#[test]
fn a_pending_cleanup_the_current_plan_does_not_exactly_name_issues_no_command() {
    let one = [target("a.service", "alpha", &["demo:A"])];
    for entry in [
        ("alpha", "unrelated.timer"),
        ("beta", "a.service"),
        ("alpha", "a.timer"),
    ] {
        let (err, calls, same) = stale(None, &[entry], &one);
        assert!(
            err.contains("is not a target of this plan"),
            "{entry:?}: {err}"
        );
        assert_eq!((calls, same), (0, true), "{entry:?}");
    }
    // The host and the unit are both in the plan, on different targets.
    let split = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.timer", "beta", &["demo:A"]),
    ];
    assert_eq!(stale(None, &[("alpha", "b.timer")], &split).1, 0);
    // A valid entry before an invalid one: nothing is switched off before every entry is checked.
    let two = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let (err, calls, same) = stale(None, &[("alpha", "a.timer"), ("alpha", "zzz.timer")], &two);
    assert!(err.contains("zzz.timer@alpha is not a target"), "{err}");
    assert_eq!((calls, same), (0, true));
}

#[test]
fn a_checkpoint_of_another_plan_or_revision_or_of_no_plan_issues_no_command() {
    let two = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let entry = [("alpha", "a.timer")];
    // Another plan, and the same targets on an earlier landed revision.
    for plan in ["another-plan".to_string(), plan_id(&two, "rev-0")] {
        let (err, calls, same) = stale(Some(&plan), &entry, &two);
        assert!(err.contains("belongs to plan"), "{err}");
        assert_eq!((calls, same), (0, true));
    }
    // Entries and no recorded plan: unknown origin. A gate without a revision binds nothing.
    let host = host_of(&[("a.timer", active())]);
    let mut state = PostState {
        cleanup: [("alpha".to_string(), "a.timer".to_string())].into(),
        ..PostState::default()
    };
    assert!(run(&two, &host, &Rows::default(), &mut state)
        .unwrap_err()
        .contains("names no plan"));
    let mut open = gate();
    open.revision = " ".into();
    let err = apply(
        &two,
        &open,
        &host,
        &Rows::default(),
        &mut PostState::default(),
        &mut |_| Ok(()),
    )
    .unwrap_err();
    assert!(err.contains("no landed revision"), "{err}");
    assert!(host.calls.borrow().is_empty());
}

#[test]
fn an_unresolved_cleanup_is_saved_blocks_the_retry_until_it_succeeds_and_is_then_cleared() {
    let mut host = svc("a.service", oneshot_ok());
    host.before_fails.insert("a.service".into());
    host.disable_fails.borrow_mut().insert("a.timer".into());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let (rows, mut state) = (Rows::default(), PostState::default());
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
    let before = host.calls.borrow().len();
    let err = run(&targets, &host, &rows, &mut state).unwrap_err();
    assert!(err.contains("could not switch off a.timer@alpha"));
    assert_eq!(&host.calls.borrow()[before..], ["disable a.timer@alpha"]);
    // Once the host lets the timer go off, the cleanup is cleared and the step goes on.
    host.disable_fails.borrow_mut().clear();
    host.before_fails.clear();
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert!(
        state.cleanup.is_empty() && r.failed.is_none(),
        "{:?}",
        r.failed
    );
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
fn a_row_write_failure_takes_the_held_timers_down_and_saves_a_failed_disable() {
    let host = host_of(&[
        ("a.service", plain("inv-1")),
        ("b.service", plain("inv-1")),
        ("a.timer", active()),
        ("b.timer", active()),
    ]);
    host.disable_fails.borrow_mut().insert("b.timer".into());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
        target("b.timer", "alpha", &["demo:B"]),
    ];
    let (mut state, mut saved) = (PostState::default(), Vec::<PostState>::new());
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
    // b.timer was installed for its service, whose rows could not be written; a.timer was already released.
    assert_eq!(disables(&host), ["disable b.timer@alpha"]);
    assert!(state
        .cleanup
        .contains(&("alpha".to_string(), "b.timer".to_string())));
    assert_eq!(saved.last().unwrap().cleanup, state.cleanup);
}

#[test]
fn a_failed_first_run_saves_its_failure_and_pending_cleanup_in_one_checkpoint() {
    let host = svc("a.service", with_result(oneshot_ok(), "exit-code"));
    host.disable_fails.borrow_mut().insert("a.timer".into());
    let targets = [
        target("a.timer", "alpha", &["demo:A"]),
        target("a.service", "alpha", &["demo:A"]),
    ];
    let (mut state, mut saved) = (PostState::default(), Vec::<PostState>::new());
    apply(
        &targets,
        &gate(),
        &host,
        &Rows::default(),
        &mut state,
        &mut |s| {
            saved.push(s.clone());
            Ok(())
        },
    )
    .unwrap();
    // One write carries both, so a lost write can never leave the cleanup without the failure.
    assert_eq!(saved.len(), 1, "{saved:?}");
    assert!(saved[0].failed.is_some() && !saved[0].cleanup.is_empty());
}
