use super::deploy::{deploy_plan, DeployPlan, Finding, PartDeploy, Probe};
use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, oneshot_ok, run, set, target, Host, Rows, T0};
use std::collections::BTreeMap;

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
            .contains(&"disable a.timer@alpha".to_string()));
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
fn a_service_gets_no_row_when_its_shipped_timer_was_refused_at_preflight() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active()); // an older, still active timer on the host
    let rows = Rows::default();
    let mut state = PostState::default();
    let mut g = gate();
    g.excluded.insert(("a.timer".into(), "alpha".into()));
    let r = apply(
        &[target("a.service", "alpha", &["demo:A"])],
        &g,
        &host,
        &rows,
        &mut state,
        &mut |_| Ok(()),
    )
    .unwrap();
    assert!(r.failed.unwrap().1.contains("refused at preflight"));
    assert!(rows.0.borrow().is_empty());
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
        .contains(&"disable a.timer@alpha".to_string()));
    // A later exit time is a new run, even a minute before the clock: no clock skew is needed to tell.
    let mut host = ran_at("2026-11-24T00:59:30Z");
    let mut old = host.runs["a.service"].clone();
    old.exec_main_exit_timestamp = Some("2026-11-24T00:59:00Z".into());
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

struct Dry(Vec<&'static str>);
impl Probe for Dry {
    fn probe(&self, host: &str, family: &str) -> Result<(), Vec<Finding>> {
        if self.0.contains(&family) {
            Err(vec![Finding {
                kind: "env".into(),
                name: format!("{host}.env"),
            }])
        } else {
            Ok(())
        }
    }
}

/// The fixture train: part 1 ships `u.service` (item demo:B, host alpha), part 2 is unitless, part 3 ships
/// `bad.service` whose dry run fails.
fn fixture_plan() -> DeployPlan {
    let part = |pr, families: &[&str], item: &str| PartDeploy {
        pr,
        families: set(families),
        items: set(&[item]),
        trailer_hosts: set(&["alpha"]),
        shipped: BTreeMap::new(),
    };
    deploy_plan(
        &[
            part(1, &["u.service"], "demo:B"),
            part(2, &[], "demo:A"),
            part(3, &["bad.service"], "demo:C"),
        ],
        &BTreeMap::new(),
        &set(&["alpha"]),
        &Dry(vec!["bad.service"]),
    )
}

#[test]
fn the_plan_carries_the_items_of_each_family_and_drops_a_refused_family_with_its_blocker() {
    let plan = fixture_plan();
    assert_eq!(plan.go_req(), "deploy: u.service@alpha");
    assert_eq!(plan.targets[0].items, set(&["demo:B"]));
    assert_eq!(plan.blockers.len(), 1);
    assert_eq!(
        plan.blockers[0].key,
        "plangraph:LE-DEPLOY-BLOCKED-BAD-SERVICE-ALPHA"
    );
}

#[test]
fn only_the_targets_the_go_row_names_are_installed() {
    let plan = fixture_plan();
    let t = named_targets(&plan, "deploy: u.service@alpha").unwrap();
    assert_eq!(t.len(), 1);
    assert_eq!(t[0].items, set(&["demo:B"]));
    assert!(named_targets(&plan, "deploy: none").unwrap().is_empty());
    let e = named_targets(&plan, "deploy: bad.service@alpha").unwrap_err();
    assert!(e.contains("not a passing target"), "{e}");
    assert!(named_targets(&plan, "no deploy here").is_err());
}

#[test]
fn a_failed_read_before_the_install_leaves_no_baseline_so_the_target_fails() {
    // The pre-install read fails (not "absent") and the unit shows an old success afterwards: without a
    // baseline nothing proves this install ran it, so no row is written.
    let mut host = ran_at("2026-11-24T00:59:00Z");
    host.before_fails.insert("a.service".into());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r.failed.unwrap().1.contains("read before install"));
    assert!(rows.0.borrow().is_empty());
    assert!(
        !host.installed.borrow().contains("a.service"),
        "installed without a baseline"
    );
}

#[test]
fn a_timers_invocation_between_the_baseline_and_the_install_is_not_proof_of_this_install() {
    // The timer ran the service after the baseline read: a newer exit time, but not the invocation the
    // install started.
    let mut host = ran_at("2026-11-24T00:59:30Z");
    host.runs.get_mut("a.service").unwrap().invocation = Some("timer-run".into());
    host.started
        .insert("a.service".into(), "install-run".into());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r
        .failed
        .unwrap()
        .1
        .contains("invocation=timer-run, started=install-run"));
    assert!(rows.0.borrow().is_empty());
    // An install that reports no invocation proves nothing either.
    let mut host = ran_at("2026-11-24T00:59:30Z");
    host.runs.get_mut("a.service").unwrap().invocation = None;
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
fn a_fractional_systemd_exit_time_is_written_as_rfc_3339_for_the_keeper() {
    assert_eq!(
        exit_rfc3339("Tue 2026-11-24 01:00:00.5 UTC").as_deref(),
        Some("2026-11-24T01:00:00.500Z")
    );
    assert_eq!(
        exit_rfc3339("Tue 2026-11-24 01:00:00 UTC").as_deref(),
        Some("2026-11-24T01:00:00Z")
    );
    let host = ran_at("Tue 2026-11-24 01:00:00 UTC");
    let (rows, mut state) = (Rows::default(), PostState::default());
    run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    let rows = rows.0.borrow();
    let service = rows.iter().find(|r| r.unit == "a.service").unwrap();
    assert_eq!(
        service.exec_main_exit_timestamp.as_deref(),
        Some("2026-11-24T01:00:00Z")
    );
}
