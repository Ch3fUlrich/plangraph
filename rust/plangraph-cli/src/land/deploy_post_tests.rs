use super::deploy::{deploy_plan, DeployPlan, Finding, PartDeploy, Probe, Target};
use super::deploy_post::*;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

fn set(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

fn target(family: &str, host: &str, items: &[&str]) -> Target {
    Target {
        family: family.into(),
        host: host.into(),
        parts: vec![1],
        items: set(items),
    }
}

fn oneshot_ok() -> RunRead {
    RunRead {
        is_active: "inactive".into(),
        result: Some("success".into()),
        exec_main_exit_timestamp: Some("2026-11-24T01:00:00Z".into()),
        oneshot: true,
    }
}

#[derive(Default)]
struct Host {
    runs: BTreeMap<String, RunRead>,
    calls: RefCell<Vec<String>>,
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
struct Rows(RefCell<Vec<DeployRow>>);

impl RowSink for Rows {
    fn write(&self, row: &DeployRow) -> Result<(), String> {
        self.0.borrow_mut().push(row.clone());
        Ok(())
    }
}

fn run(
    targets: &[Target],
    host: &Host,
    rows: &Rows,
    state: &mut PostState,
) -> Result<PostReport, String> {
    apply(targets, host, rows, state, &mut |_| Ok(()))
}

#[test]
fn a_good_first_run_writes_one_deploy_row_per_item() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
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
    assert_eq!(
        rows.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        ["demo:A", "demo:B"]
    );
    assert_eq!(rows[0].unit, "a.service");
    assert_eq!(rows[0].result.as_deref(), Some("success"));
    assert_eq!(rows[0].kind.as_deref(), Some("oneshot"));
    assert!(state.done.contains("a.service@alpha"));
}

#[test]
fn a_failed_first_run_disables_the_timer_writes_no_row_and_stops_the_rest() {
    let mut host = Host::default();
    let mut bad = oneshot_ok();
    bad.result = Some("exit-code".into());
    host.runs.insert("a.service".into(), bad);
    host.runs.insert("b.service".into(), oneshot_ok());
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
    assert!(ok.good("bin:tool"));
    assert!(!ok.good("a.service"));
}

#[test]
fn a_rerun_skips_finished_targets_and_a_recorded_failure_blocks_it() {
    let mut host = Host::default();
    host.runs.insert("a.service".into(), oneshot_ok());
    host.runs.insert("b.service".into(), oneshot_ok());
    let rows = Rows::default();
    let mut state = PostState::default();
    state.done.insert("a.service@alpha".into());
    let targets = [
        target("a.service", "alpha", &["demo:A"]),
        target("b.service", "alpha", &["demo:B"]),
    ];
    let mut saved = vec![];
    let r = apply(&targets, &host, &rows, &mut state, &mut |s| {
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
