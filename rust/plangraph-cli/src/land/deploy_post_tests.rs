use super::deploy::{deploy_plan, DeployPlan, Finding, PartDeploy, Probe, Target};
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

#[derive(Default)]
pub(super) struct Host {
    pub(super) runs: BTreeMap<String, RunRead>,
    pub(super) calls: RefCell<Vec<String>>,
}

impl Installer for Host {
    fn install(&self, host: &str, family: &str) -> Result<(), String> {
        self.calls
            .borrow_mut()
            .push(format!("install {family}@{host}"));
        Ok(())
    }
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String> {
        self.calls
            .borrow_mut()
            .push(format!("read {family}@{host}"));
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

/// Landed head at epoch 1e9; the oneshot fixture exited on 2026-11-24; "now" is far later.
pub(super) fn gate() -> Gate {
    Gate {
        fresh_after: 1_000_000_000,
        now: 4_000_000_000,
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
    assert!(ok.good("bin:tool", &gate()));
    assert!(!ok.good("a.service", &gate()));
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
fn a_shipped_timer_goes_first_and_is_not_written_twice() {
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
    assert_eq!(r.deployed, ["a.timer@alpha", "a.service@alpha"]);
    let units: Vec<_> = rows.0.borrow().iter().map(|r| r.unit.clone()).collect();
    assert_eq!(units, ["a.timer", "a.service"]);
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
    assert!(oneshot.good("a.service", &gate()));
    let bin: RunRead = serde_json::from_str(r#"{"is_active":"installed"}"#).unwrap();
    assert!(bin.good("bin:tool", &gate()));
    let svc: RunRead = serde_json::from_str(r#"{"is_active":"active"}"#).unwrap();
    assert!(svc.good("a.service", &gate()) && svc.good("a.timer", &gate()));
}
