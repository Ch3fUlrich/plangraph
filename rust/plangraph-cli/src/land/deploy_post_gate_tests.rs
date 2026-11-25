use super::deploy_post::*;
use super::deploy_post_tests::{active, oneshot_ok, run, target, Host, Rows, T0};

fn ran_at(timestamp: &str) -> Host {
    let mut host = Host::default();
    let mut run = oneshot_ok();
    run.exec_main_exit_timestamp = Some(timestamp.into());
    host.runs.insert("a.service".into(), run);
    host.runs.insert("a.timer".into(), active());
    host
}

#[test]
fn an_unparsable_stale_or_future_exit_time_is_a_failed_first_run_through_the_disable_path() {
    for bad in [
        "t",
        "",
        "2001-01-01T00:00:00Z",        // before the landed head (stale run)
        "2099-01-01T00:00:00Z",        // after "now"
        "2026-11-24T01:00:03Z",        // three seconds ahead: the keeper rejects it
        "2026-11-24T01:00:00.9Z",      // a fraction ahead: the keeper keeps the fraction
        "Mon 2001-09-09 01:46:39 UTC", // stale, systemd form
    ] {
        let host = ran_at(bad);
        let rows = Rows::default();
        let mut state = PostState::default();
        let r = run(
            &[target("a.service", "alpha", &["demo:A"])],
            &host,
            &rows,
            &mut state,
        )
        .unwrap();
        assert!(r.failed.is_some(), "{bad:?} must fail");
        assert!(rows.0.borrow().is_empty(), "{bad:?} wrote a row");
        assert!(host
            .calls
            .borrow()
            .iter()
            .all(|c| !c.starts_with("disable")));
    }
    let host = ran_at("Tue 2026-11-24 01:00:00 UTC");
    let rows = Rows::default();
    let mut state = PostState::default();
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert_eq!(r.deployed, ["a.service@alpha"]);
}

#[test]
fn exit_micros_reads_both_systemd_forms() {
    assert_eq!(
        exit_micros("2026-11-24T01:00:00Z"),
        exit_micros("Tue 2026-11-24 01:00:00 UTC")
    );
    assert_eq!(exit_micros("1970-01-01T00:00:10Z"), Some(10_000_000));
    assert_eq!(exit_micros("1970-01-01T00:00:10.25Z"), Some(10_250_000));
    assert_eq!(
        exit_micros("Tue 2026-11-24 01:00:00.5 UTC"),
        Some(T0 + 500_000)
    );
    assert_eq!(exit_micros("t"), None);
}

#[test]
fn a_slow_install_does_not_make_its_own_result_future_dated() {
    // The install takes two seconds and the oneshot exits at its end: the clock is read after the read.
    let mut host = Host {
        install_takes: 2,
        ..Host::default()
    };
    let mut run_read = oneshot_ok();
    run_read.exec_main_exit_timestamp = Some("2026-11-24T01:00:02Z".into());
    host.runs.insert("a.service".into(), run_read);
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert_eq!(r.deployed, ["a.service@alpha"], "{:?}", r.failed);
}

#[test]
fn a_result_from_before_this_install_is_not_this_installs_first_run() {
    // The unit already showed this exit time before the install: the install did not run it again.
    let mut host = ran_at("2026-11-24T00:59:00Z");
    host.before
        .insert("a.service".into(), host.runs["a.service"].clone());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r.failed.is_some());
    assert!(rows.0.borrow().is_empty());
    assert!(host
        .calls
        .borrow()
        .iter()
        .all(|c| !c.starts_with("disable")));
    // A later exit time is a new run, even a minute before the clock: no clock skew is needed to tell.
    let mut host = ran_at("2026-11-24T00:59:30Z");
    let mut old = host.runs["a.service"].clone();
    old.exec_main_exit_timestamp = Some("2026-11-24T00:59:00Z".into());
    old.invocation = Some("inv-0".into());
    host.before.insert("a.service".into(), old);
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert_eq!(r.deployed, ["a.service@alpha"], "{:?}", r.failed);
    assert_eq!(T0, exit_micros("2026-11-24T01:00:00Z").unwrap());
}

#[test]
fn a_shipped_timer_leaves_no_evidence_when_its_service_fails() {
    // Timer shipped by item B, service by item A; the service's first run fails.
    let mut host = Host::default();
    let mut bad = oneshot_ok();
    bad.result = Some("exit-code".into());
    host.runs.insert("a.service".into(), bad);
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:B"]),
    ];
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert_eq!(r.failed.as_ref().unwrap().0, "a.service@alpha");
    assert!(
        rows.0.borrow().is_empty(),
        "the timer row was written before its service passed"
    );
    assert!(state.done.is_empty(), "the timer was checkpointed done");
    assert!(host
        .calls
        .borrow()
        .contains(&"disable a.timer@alpha".to_string()));
}

#[test]
fn a_held_timer_is_recorded_once_its_service_passes() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:B"]),
    ];
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert_eq!(r.deployed, ["a.service@alpha", "a.timer@alpha"]);
    let mut got: Vec<_> = rows
        .0
        .borrow()
        .iter()
        .map(|r| (r.key.clone(), r.unit.clone()))
        .collect();
    got.sort();
    assert_eq!(
        got,
        [
            ("demo:A", "a.service"),
            ("demo:A", "a.timer"),
            ("demo:B", "a.timer")
        ]
        .map(|(k, u)| (k.to_string(), u.to_string()))
    );
    assert!(state.done.contains("a.timer@alpha") && state.done.contains("a.service@alpha"));
}
