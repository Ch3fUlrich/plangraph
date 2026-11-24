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

fn active() -> RunRead {
    RunRead {
        is_active: "active".into(),
        result: None,
        exec_main_exit_timestamp: None,
        oneshot: false,
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
    host.runs.insert("b.timer".into(), active());
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

fn sh(script: &str) -> Vec<String> {
    vec!["sh".into(), "-c".into(), script.into()]
}

#[test]
fn the_command_installer_runs_install_read_and_disable_with_host_and_name() {
    let log = tempfile::tempdir().unwrap();
    let path = log.path().join("calls");
    let script = format!(
        "echo \"$0 $1 $2\" >> {}; [ \"$0\" = read ] && echo '{{\"is_active\":\"active\"}}'; exit 0",
        path.display()
    );
    let inst = CommandInstaller { argv: sh(&script) };
    inst.install("alpha", "a.service").unwrap();
    assert_eq!(
        inst.read_run("alpha", "a.timer").unwrap().is_active,
        "active"
    );
    inst.disable("alpha", "a.timer").unwrap();
    assert_eq!(
        std::fs::read_to_string(path).unwrap(),
        "install alpha a.service\nread alpha a.timer\ndisable alpha a.timer\n"
    );
    let bad = CommandInstaller { argv: sh("exit 3") };
    assert!(bad
        .install("alpha", "a.service")
        .unwrap_err()
        .contains("exited"));
    let junk = CommandInstaller {
        argv: sh("echo nope"),
    };
    assert!(junk
        .read_run("alpha", "a.service")
        .unwrap_err()
        .contains("unreadable"));
    assert!(CommandInstaller { argv: vec![] }.install("h", "f").is_err());
}

#[test]
fn the_command_rows_pass_the_row_as_json_on_stdin_with_the_key_as_argument() {
    let dir = tempfile::tempdir().unwrap();
    let rows = CommandRows {
        argv: sh(&format!("cat > {}/\"$0\"", dir.path().display())),
    };
    rows.write(&DeployRow {
        key: "demo-B".into(),
        host: "alpha".into(),
        unit: "a.timer".into(),
        is_active: "active".into(),
        kind: None,
        result: None,
        exec_main_exit_timestamp: None,
    })
    .unwrap();
    let got: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.path().join("demo-B")).unwrap()).unwrap();
    assert_eq!(got["unit"], "a.timer");
    assert_eq!(got["host"], "alpha");
    assert!(CommandRows { argv: sh("exit 1") }
        .write(&DeployRow {
            key: "k".into(),
            host: "h".into(),
            unit: "u".into(),
            is_active: "active".into(),
            kind: None,
            result: None,
            exec_main_exit_timestamp: None,
        })
        .is_err());
}

fn script(dir: &std::path::Path, name: &str, body: &str) -> String {
    use std::os::unix::fs::PermissionsExt;
    let p = dir.join(name);
    std::fs::write(&p, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    p.display().to_string()
}

#[test]
fn deploy_apply_end_to_end_writes_rows_then_resumes_and_a_failure_blocks_the_rerun() {
    let dir = tempfile::tempdir().unwrap();
    let d = dir.path();
    let plan = DeployPlan {
        targets: vec![target("a.service", "alpha", &["demo:B"])],
        blockers: vec![],
    };
    std::fs::write(d.join("plan.json"), serde_json::to_vec(&plan).unwrap()).unwrap();
    let rows_dir = d.join("rows");
    std::fs::create_dir(&rows_dir).unwrap();
    let install = script(
        d,
        "install.sh",
        r#"if [ "$1" = read ]; then
  if [ "$3" = a.timer ]; then echo '{"is_active":"active"}'; else echo '{"is_active":"inactive","result":"success","exec_main_exit_timestamp":"t","oneshot":true}'; fi
fi
exit 0"#,
    );
    let row_cmd = script(
        d,
        "row.sh",
        &format!("cat >> {}/\"$1\"", rows_dir.display()),
    );
    let mut flags: BTreeMap<String, String> = BTreeMap::new();
    for (k, v) in [
        ("plan", d.join("plan.json").display().to_string()),
        ("go", "deploy: a.service@alpha".to_string()),
        ("state", d.join("state.json").display().to_string()),
        ("install-cmd", install),
        ("row-cmd", row_cmd),
    ] {
        flags.insert(k.into(), v);
    }
    let lines = super::deploy_post::run_cli(&flags).unwrap();
    assert_eq!(lines, ["deployed a.service@alpha"]);
    let written = std::fs::read_to_string(rows_dir.join("demo:B")).unwrap();
    assert_eq!(written.matches("\"unit\"").count(), 2, "{written}");
    // A rerun resumes: the target is done and nothing is written again.
    let again = super::deploy_post::run_cli(&flags).unwrap();
    assert_eq!(again, ["already done a.service@alpha"]);
    assert_eq!(
        std::fs::read_to_string(rows_dir.join("demo:B")).unwrap(),
        written
    );
    // A failed first run is an error, saved in the state, and blocks the next run.
    std::fs::remove_file(d.join("state.json")).unwrap();
    flags.insert("install-cmd".into(), script(d, "bad.sh", "exit 1"));
    let e = super::deploy_post::run_cli(&flags).unwrap_err();
    assert!(e.contains("stop: a.service@alpha"), "{e}");
    let blocked = super::deploy_post::run_cli(&flags).unwrap_err();
    assert!(blocked.contains("earlier first run failed"), "{blocked}");
}
