use super::deploy::Target;
use super::deploy_post::*;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn set(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

pub(super) fn target(family: &str, host: &str, items: &[&str]) -> Target {
    Target {
        family: family.into(),
        host: host.into(),
        parts: vec![1],
        items: set(items),
    }
}

pub(super) fn active() -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: None,
        exec_main_exit_timestamp: None,
        oneshot: false,
    }
}

pub(super) fn oneshot_ok() -> RunRead {
    RunRead {
        is_active: "inactive".into(),
        result: Some("success".into()),
        exec_main_exit_timestamp: Some("2026-11-24T01:00:00Z".into()),
        oneshot: true,
    }
}

/// The fixture oneshot exits at this instant; the test clock starts there.
pub(super) const T0: i64 = 1_795_482_000; // 2026-11-24T01:00:00Z

thread_local! {
    static NOW: std::cell::Cell<i64> = const { std::cell::Cell::new(T0) };
}

pub(super) fn clock() -> i64 {
    NOW.with(|n| n.get())
}

#[derive(Default)]
pub(super) struct Host {
    pub(super) runs: BTreeMap<String, RunRead>,
    pub(super) calls: RefCell<Vec<String>>,
    /// Seconds the test clock advances while an install runs.
    pub(super) install_takes: i64,
    /// Targets whose read fails.
    pub(super) unreadable: BTreeSet<String>,
}

impl Installer for Host {
    fn install(&self, host: &str, family: &str) -> Result<(), String> {
        self.calls
            .borrow_mut()
            .push(format!("install {family}@{host}"));
        NOW.with(|n| n.set(n.get() + self.install_takes));
        Ok(())
    }
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String> {
        self.calls
            .borrow_mut()
            .push(format!("read {family}@{host}"));
        if self.unreadable.contains(family) {
            return Err("unreadable".into());
        }
        self.runs
            .get(family)
            .cloned()
            .ok_or_else(|| "no run".into())
    }
    fn disable(&self, host: &str, unit: &str) -> Result<(), String> {
        self.calls
            .borrow_mut()
            .push(format!("disable {unit}@{host}"));
        Ok(())
    }
}

#[derive(Default)]
pub(super) struct Rows(pub(super) RefCell<Vec<DeployRow>>);

impl RowSink for Rows {
    fn write(&self, row: &DeployRow) -> Result<(), String> {
        self.0.borrow_mut().push(row.clone());
        Ok(())
    }
}

/// Landed head at epoch 1e9; the test clock is at [`T0`], the instant the oneshot fixture exited.
pub(super) fn gate() -> Gate {
    NOW.with(|n| n.set(T0));
    Gate {
        fresh_after: 1_000_000_000,
        clock,
        excluded: BTreeSet::new(),
    }
}

pub(super) fn run(
    targets: &[Target],
    host: &Host,
    rows: &Rows,
    state: &mut PostState,
) -> Result<PostReport, String> {
    apply(targets, &gate(), host, rows, state, &mut |_| Ok(()))
}

#[test]
fn a_good_first_run_writes_one_deploy_row_per_item() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    let r = run(
        &[target("a.service", "alpha", &["demo:A", "demo:B"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert_eq!(r.deployed, ["a.service@alpha"]);
    let rows = rows.0.borrow();
    let got: Vec<_> = rows
        .iter()
        .map(|r| (r.key.as_str(), r.unit.as_str()))
        .collect();
    // The keeper gate wants the active timer row beside the oneshot service row, for every item.
    assert_eq!(
        got,
        [
            ("demo:A", "a.timer"),
            ("demo:A", "a.service"),
            ("demo:B", "a.timer"),
            ("demo:B", "a.service")
        ]
    );
    assert_eq!(rows[0].is_active, "active");
    assert_eq!(rows[1].result.as_deref(), Some("success"));
    assert_eq!(rows[1].kind.as_deref(), Some("oneshot"));
    assert!(state.done.contains("a.service@alpha"));
}

#[test]
fn a_failed_first_run_disables_the_timer_writes_no_row_and_stops_the_rest() {
    let mut host = Host::default();
    let mut bad = oneshot_ok();
    bad.result = Some("exit-code".into());
    host.runs.insert("a.service".into(), bad);
    host.runs.insert("b.service".into(), oneshot_ok());
    host.runs.insert("b.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert!(
        rows.0.borrow().is_empty(),
        "no row after a failed first run"
    );
    assert_eq!(r.failed.as_ref().unwrap().0, "a.service@alpha");
    assert_eq!(r.not_attempted, ["b.service@alpha"]);
    let calls = host.calls.borrow();
    assert!(
        calls.contains(&"disable a.timer@alpha".to_string()),
        "{calls:?}"
    );
    assert!(!calls.iter().any(|c| c.contains("b.service")), "{calls:?}");
    assert_eq!(state.failed.as_deref(), Some("a.service@alpha"));
}

#[test]
fn an_install_failure_is_a_failed_first_run() {
    struct Broken;
    impl Installer for Broken {
        fn install(&self, _: &str, _: &str) -> Result<(), String> {
            Err("no space".into())
        }
        fn read_run(&self, _: &str, _: &str) -> Result<RunRead, String> {
            unreachable!("nothing to read after a failed install")
        }
        fn disable(&self, _: &str, _: &str) -> Result<(), String> {
            Ok(())
        }
    }
    let rows = Rows::default();
    let mut state = PostState::default();
    let r = apply(
        &[target("a.timer", "alpha", &["demo:A"])],
        &gate(),
        &Broken,
        &rows,
        &mut state,
        &mut |_| Ok(()),
    )
    .unwrap();
    assert!(r.failed.unwrap().1.contains("no space"));
    assert!(rows.0.borrow().is_empty());
}

#[test]
fn a_binary_needs_installed_and_has_no_timer_to_disable() {
    assert_eq!(unit_to_disable("bin:tool"), None);
    assert_eq!(unit_to_disable("a.service").as_deref(), Some("a.timer"));
    assert_eq!(unit_to_disable("a.timer").as_deref(), Some("a.timer"));
    let ok = RunRead {
        is_active: "installed".into(),
        result: None,
        exec_main_exit_timestamp: None,
        oneshot: false,
    };
    assert!(ok.good("bin:tool", &gate(), T0, T0));
    assert!(!ok.good("a.service", &gate(), T0, T0));
}

#[test]
fn a_rerun_skips_finished_targets_and_a_recorded_failure_blocks_it() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("b.service".into(), oneshot_ok());
    host.runs.insert("b.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    state.done.insert("a.service@alpha".into());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let mut saved = vec![];
    let r = apply(&targets, &gate(), &host, &rows, &mut state, &mut |s| {
        saved.push(s.clone());
        Ok(())
    })
    .unwrap();
    assert_eq!(r.skipped_done, ["a.service@alpha"]);
    assert_eq!(r.deployed, ["b.service@alpha"]);
    assert!(!host.calls.borrow().iter().any(|c| c.contains("a.service")));
    assert_eq!(saved.len(), 1, "state is saved after each finished target");
    state.failed = Some("c.service@alpha".into());
    assert!(run(&targets, &host, &rows, &mut state).is_err());
}

#[test]
fn a_good_oneshot_with_an_inactive_timer_is_a_failed_first_run() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    let mut off = active();
    off.is_active = "inactive".into();
    host.runs.insert("a.timer".into(), off);
    let rows = Rows::default();
    let mut state = PostState::default();
    let r = run(
        &[target("a.service", "alpha", &["demo:A"])],
        &host,
        &rows,
        &mut state,
    )
    .unwrap();
    assert!(r.failed.unwrap().1.contains("paired timer a.timer"));
    assert!(
        rows.0.borrow().is_empty(),
        "no service row without its active timer"
    );
    assert!(host
        .calls
        .borrow()
        .contains(&"disable a.timer@alpha".to_string()));
}

#[test]
fn a_shipped_timer_is_installed_first_but_recorded_after_its_service_and_not_written_twice() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:A"]),
    ];
    let r = run(&targets, &host, &rows, &mut state).unwrap();
    assert_eq!(r.deployed, ["a.service@alpha", "a.timer@alpha"]);
    assert!(
        host.calls
            .borrow()
            .iter()
            .position(|c| c == "install a.timer@alpha")
            < host
                .calls
                .borrow()
                .iter()
                .position(|c| c == "install a.service@alpha")
    );
    let units: Vec<_> = rows.0.borrow().iter().map(|r| r.unit.clone()).collect();
    assert_eq!(units, ["a.service", "a.timer"]);
}

#[test]
fn a_timer_shipped_for_other_items_still_gets_a_row_for_every_service_item() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("a.timer".into(), active());
    let rows = Rows::default();
    let mut state = PostState::default();
    // Part 1 ships the service (item A), part 2 ships its timer (item B).
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("a.timer", "alpha", &["demo:B"]),
    ];
    run(&targets, &host, &rows, &mut state).unwrap();
    let mut got: Vec<_> = rows
        .0
        .borrow()
        .iter()
        .map(|r| (r.key.clone(), r.unit.clone()))
        .collect();
    got.sort();
    let want = [
        ("demo:A", "a.service"),
        ("demo:A", "a.timer"),
        ("demo:B", "a.timer"),
    ];
    assert_eq!(got, want.map(|(k, u)| (k.to_string(), u.to_string())));
}

/// The JSON the landing-train doc tells a read command to print.
#[test]
fn the_documented_read_json_is_judged_good() {
    let oneshot: RunRead = serde_json::from_str(
        r#"{"is_active":"inactive","oneshot":true,"result":"success","exec_main_exit_timestamp":"2026-11-24T01:00:00Z"}"#,
    )
    .unwrap();
    assert!(oneshot.good("a.service", &gate(), T0, T0));
    let bin: RunRead = serde_json::from_str(r#"{"is_active":"installed"}"#).unwrap();
    assert!(bin.good("bin:tool", &gate(), T0, T0));
    let svc: RunRead = serde_json::from_str(r#"{"is_active":"active"}"#).unwrap();
    assert!(svc.good("a.service", &gate(), T0, T0) && svc.good("a.timer", &gate(), T0, T0));
}
