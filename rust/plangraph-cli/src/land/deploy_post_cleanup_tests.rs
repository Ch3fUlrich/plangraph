use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, oneshot_ok, run, target, Host, Rows, T0};

fn disables(host: &Host) -> Vec<String> {
    host.calls
        .borrow()
        .iter()
        .filter(|c| c.starts_with("disable"))
        .cloned()
        .collect()
}

fn service_run(invocation: &str, result: &str) -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: Some(result.into()),
        exec_main_exit_timestamp: None,
        oneshot: false,
        invocation: Some(invocation.into()),
    }
}

#[test]
fn a_refused_paired_timer_is_checked_before_any_host_change() {
    let mut host = Host::default();
    host.runs
        .insert("b.service".into(), service_run("inv-1", "success"));
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let mut g = gate();
    g.excluded.insert(("a.timer".into(), "alpha".into()));
    let targets = [
        target("b.service", "alpha", &["demo:B"]),
        target("a.service", "alpha", &["demo:A"]),
    ];
    let r = apply(&targets, &g, &host, &rows, &mut state, &mut |_| Ok(())).unwrap();
    assert_eq!(r.failed.as_ref().unwrap().0, "a.service@alpha");
    assert!(r.failed.unwrap().1.contains("refused at preflight"));
    assert!(host.calls.borrow().is_empty(), "{:?}", host.calls.borrow());
    assert!(rows.0.borrow().is_empty());
    assert!(state.done.is_empty() && state.failed.is_none());
}

#[test]
fn a_failed_service_disables_no_timer_the_go_does_not_name() {
    // The timer exists on the host but is not a target: this attempt did not touch it.
    let mut host = Host::default();
    let mut bad = oneshot_ok();
    bad.result = Some("exit-code".into());
    host.runs.insert("a.service".into(), bad);
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r.failed.is_some());
    assert!(disables(&host).is_empty(), "{:?}", host.calls.borrow());
    assert_eq!(state.failed.as_deref(), Some("a.service@alpha"));
}

#[test]
fn a_failed_service_disables_only_the_timer_this_attempt_installed() {
    let mut host = Host::default();
    let mut bad = oneshot_ok();
    bad.result = Some("exit-code".into());
    host.runs.insert("a.service".into(), bad);
    host.runs.insert("a.timer".into(), active());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    run(&targets, &host, &rows, &mut state).unwrap();
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
}

#[test]
fn a_failed_baseline_read_changes_nothing_and_leaves_the_rerun_open() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
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
    assert_eq!(*host.calls.borrow(), ["read a.service@alpha"]);
    assert!(
        state.failed.is_none(),
        "nothing changed, so the rerun stays open"
    );
    assert!(rows.0.borrow().is_empty());
}

#[test]
fn a_failed_baseline_read_still_cleans_up_the_timer_installed_earlier_in_the_attempt() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    host.before_fails.insert("a.service".into());
    let (rows, mut state) = (Rows::default(), PostState::default());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert!(r.failed.is_some());
    let calls = host.calls.borrow();
    assert!(!calls.contains(&"install a.service@alpha".to_string()));
    assert_eq!(disables(&host), ["disable a.timer@alpha"]);
    assert!(state.failed.is_none() && state.done.is_empty());
}

#[test]
fn a_plain_service_needs_a_fresh_invocation_and_a_successful_start() {
    let case = |run_read: RunRead, started: Option<&str>, before: Option<RunRead>| {
        let mut host = Host::default();
        host.runs.insert("a.service".into(), run_read);
        match started {
            Some(s) => {
                host.started.insert("a.service".into(), s.into());
            }
            None => {
                host.silent_install.insert("a.service".into());
            }
        }
        if let Some(b) = before {
            host.before.insert("a.service".into(), b);
        }
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
    // A fresh invocation that started and succeeded is recorded.
    assert_eq!(
        case(
            service_run("inv-2", "success"),
            Some("inv-2"),
            Some(service_run("inv-1", "success"))
        ),
        (true, 1)
    );
    // The unit shows the invocation it had before the install: nothing restarted it.
    assert_eq!(
        case(
            service_run("inv-1", "success"),
            Some("inv-1"),
            Some(service_run("inv-1", "success"))
        ),
        (false, 0)
    );
    // Not the invocation this install started (a restart by something else), or none reported.
    assert_eq!(
        case(service_run("inv-3", "success"), Some("inv-2"), None),
        (false, 0)
    );
    assert_eq!(
        case(service_run("inv-2", "success"), None, None),
        (false, 0)
    );
    // Active but its start did not succeed (a restart loop, start limit).
    assert_eq!(
        case(service_run("inv-2", "start-limit-hit"), Some("inv-2"), None),
        (false, 0)
    );
}

#[test]
fn a_binary_needs_installed() {
    let ok = RunRead {
        is_active: "installed".into(),
        result: None,
        exec_main_exit_timestamp: None,
        oneshot: false,
        invocation: None,
    };
    assert!(ok.good("bin:tool", &gate(), &Baseline::default(), T0, Some("inv-1")));
    assert!(!ok.good(
        "a.service",
        &gate(),
        &Baseline::default(),
        T0,
        Some("inv-1")
    ));
}

/// The JSON the landing-train doc tells a read command to print.
#[test]
fn the_documented_read_json_is_judged_good() {
    let oneshot: RunRead = serde_json::from_str(
        r#"{"is_active":"inactive","oneshot":true,"result":"success","exec_main_exit_timestamp":"2026-11-24T01:00:00Z","invocation":"inv-1"}"#,
    )
    .unwrap();
    assert!(oneshot.good(
        "a.service",
        &gate(),
        &Baseline::default(),
        T0,
        Some("inv-1")
    ));
    let bin: RunRead = serde_json::from_str(r#"{"is_active":"installed"}"#).unwrap();
    assert!(bin.good("bin:tool", &gate(), &Baseline::default(), T0, Some("inv-1")));
    let svc: RunRead =
        serde_json::from_str(r#"{"is_active":"active","result":"success","invocation":"inv-1"}"#)
            .unwrap();
    assert!(
        svc.good(
            "a.service",
            &gate(),
            &Baseline::default(),
            T0,
            Some("inv-1")
        ) && svc.good("a.timer", &gate(), &Baseline::default(), T0, Some("inv-1"))
    );
}
