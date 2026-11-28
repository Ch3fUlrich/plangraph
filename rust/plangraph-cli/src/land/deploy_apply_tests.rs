use super::deploy_apply::*;
use super::deploy_host::{parse_show, Step, Sys};
use super::deploy_post::*;
use super::deploy_post_tests::{active, gate, host_of, oneshot_ok, target, Rows, REV};
use std::cell::RefCell;
use std::path::PathBuf;

#[test]
fn the_dry_run_names_every_target_changes_nothing_and_refuses_another_plans_state() {
    let targets = [
        target("bin:app", "k", &["demo:A"]),
        target("pg-x.timer", "k", &[]),
    ];
    let mut state = PostState::default();
    let first = dry_run(&targets, REV, &state).unwrap();
    assert_eq!(first, dry_run(&targets, REV, &state).unwrap());
    assert_eq!(state, PostState::default());
    assert_eq!(
        &first[1..],
        [
            "would install bin:app@k rows demo:A",
            "would install pg-x.timer@k rows -"
        ]
    );
    state.plan = plan_id(&targets, REV);
    state.done.insert("bin:app@k".into());
    assert_eq!(
        dry_run(&targets, REV, &state).unwrap()[1],
        "already done bin:app@k"
    );
    let err = dry_run(&targets, "rev-2", &state).unwrap_err();
    assert!(err.contains("belongs to plan"), "{err}");
}

#[test]
fn a_second_apply_installs_nothing_and_writes_no_row_twice() {
    let targets = [target("bin:app", "k", &["demo:A"])];
    let mut installed = active();
    installed.is_active = "installed".into();
    let host = host_of(&[("bin:app", installed)]);
    let (rows, mut state) = (Rows::default(), PostState::default());
    apply(&targets, &gate(), &host, &rows, &mut state, &mut |_| Ok(())).unwrap();
    let again = apply(&targets, &gate(), &host, &rows, &mut state, &mut |_| Ok(())).unwrap();
    assert_eq!(again.skipped_done, ["bin:app@k"]);
    let installs = host
        .calls
        .borrow()
        .iter()
        .filter(|c| c.starts_with("install"))
        .count();
    assert_eq!(installs, 1);
    assert_eq!(rows.0.borrow().len(), 1);
}

#[test]
fn deploy_rows_carry_the_fields_the_keeper_deploy_verifier_reads() {
    let bin = DeployRow {
        key: "plangraph:K".into(),
        host: "k".into(),
        unit: "bin:app".into(),
        is_active: "installed".into(),
        kind: None,
        result: None,
        exec_main_exit_timestamp: None,
    };
    assert_eq!(
        serde_json::to_value(&bin).unwrap(),
        serde_json::json!({"key": "plangraph:K", "host": "k", "unit": "bin:app", "is_active": "installed"})
    );
    let run = oneshot_ok();
    let shot = DeployRow {
        unit: "pg-x.service".into(),
        is_active: run.is_active,
        kind: Some("oneshot".into()),
        result: run.result,
        exec_main_exit_timestamp: run.exec_main_exit_timestamp,
        ..bin
    };
    let value = serde_json::to_value(&shot).unwrap();
    assert_eq!(value["type"], "oneshot");
    assert_eq!(value["result"], "success");
    assert_eq!(value["exec_main_exit_timestamp"], "2026-11-24T01:00:00Z");
    let evidence: serde_json::Value =
        serde_json::from_str(&CommandRows::evidence_ref(&shot).unwrap()).unwrap();
    assert!(evidence.get("key").is_none() && evidence["unit"] == "pg-x.service");
    let rows = CommandRows {
        template: [
            "pg", "evidence", "{key}", "--kind", "deploy", "--ref", "{ref}",
        ]
        .map(String::from)
        .to_vec(),
    };
    assert_eq!(
        rows.argv("plangraph:K", "{\"a\":1}"),
        [
            "pg",
            "evidence",
            "plangraph:K",
            "--kind",
            "deploy",
            "--ref",
            "{\"a\":1}"
        ]
    );
}

#[test]
fn host_steps_run_locally_on_the_self_host_and_over_ssh_or_wsl_elsewhere() {
    let mut registry = pg_worker_core::hosts::HostRegistry::from_json_str(include_str!(
        "../../../../docs/schemas/examples/hosts.example.json"
    ))
    .unwrap();
    registry.self_host = "workstation".into();
    let cmd = HostCmd {
        registry,
        repo_id: "plangraph".into(),
        rev: "r1".into(),
    };
    let mine = cmd.argv("workstation", "read", "bin:app").unwrap();
    assert_eq!(&mine[1..5], ["land-train", "deploy-host", "--op", "read"]);
    let wsl = cmd.argv("code", "install", "pg-x.timer").unwrap();
    assert_eq!(&wsl[..2], ["ssh", "-o"]);
    assert!(
        wsl.contains(&"wsl".to_string())
            && wsl
                .last()
                .unwrap()
                .starts_with("plangraph land-train deploy-host --op install"),
        "{wsl:?}"
    );
    assert!(cmd
        .argv("compact", "read", "x")
        .unwrap_err()
        .contains("no plangraph checkout"));
    assert!(cmd.argv("nowhere", "read", "x").is_err());
}

/// A host whose commands all succeed; records them.
struct FakeSys {
    home: PathBuf,
    calls: RefCell<Vec<String>>,
    landed: bool,
}

impl Sys for FakeSys {
    fn run(&self, argv: &[&str]) -> Result<(bool, String), String> {
        self.calls.borrow_mut().push(argv.join(" "));
        Ok(match argv {
            [_, _, _, "merge-base", ..] => (self.landed, String::new()),
            [_, _, _, "rev-parse", "HEAD"] => (true, "h1\n".into()),
            [.., "InvocationID", "--value", _] => (true, "inv-9\n".into()),
            _ => (true, String::new()),
        })
    }
    fn home(&self) -> PathBuf {
        self.home.clone()
    }
}

fn repo_with_unit() -> tempfile::TempDir {
    let repo = tempfile::tempdir().unwrap();
    let manifest = "[[unit]]\nname = \"pg-x\"\nfiles = [\"pg-x.service\", \"pg-x.timer\"]\nhosts = [\"keeper\"]\nenv_files = [\"x.env\"]\nfirst_run = \"it runs\"\n";
    std::fs::create_dir_all(repo.path().join("deploy/systemd")).unwrap();
    std::fs::write(repo.path().join("deploy/manifest.toml"), manifest).unwrap();
    std::fs::write(
        repo.path().join("deploy/systemd/pg-x.service"),
        "[Service]\nExecStart=%h/.local/bin/pg-x run\n",
    )
    .unwrap();
    std::fs::write(repo.path().join("deploy/systemd/pg-x.timer"), "[Timer]\n").unwrap();
    repo
}

#[test]
fn the_host_dry_run_names_what_is_missing_and_is_silent_on_a_complete_host() {
    let (repo, home) = (repo_with_unit(), tempfile::tempdir().unwrap());
    let sys = FakeSys {
        home: home.path().into(),
        calls: RefCell::default(),
        landed: true,
    };
    let step = Step {
        sys: &sys,
        repo: repo.path().into(),
        rev: "r1".into(),
    };
    assert_eq!(
        step.check("pg-x.timer").unwrap(),
        [
            format!("MISSING binary {}/.local/bin/pg-x", home.path().display()),
            "MISSING env x.env".into()
        ]
    );
    std::fs::create_dir_all(home.path().join(".config/plangraph")).unwrap();
    std::fs::write(home.path().join(".config/plangraph/x.env"), "").unwrap();
    std::fs::create_dir_all(home.path().join(".local/bin")).unwrap();
    std::fs::write(home.path().join(".local/bin/pg-x"), "").unwrap();
    assert!(step.check("pg-x.timer").unwrap().is_empty());
    assert!(step.check("pg-other.service").is_err());
}

#[test]
fn a_host_install_copies_the_family_enables_and_restarts_only_on_a_landed_checkout() {
    let (repo, home) = (repo_with_unit(), tempfile::tempdir().unwrap());
    let sys = FakeSys {
        home: home.path().into(),
        calls: RefCell::default(),
        landed: true,
    };
    let step = Step {
        sys: &sys,
        repo: repo.path().into(),
        rev: "r1".into(),
    };
    assert_eq!(
        step.install("pg-x.service").unwrap().as_deref(),
        Some("inv-9")
    );
    assert_eq!(
        step.install("pg-x.service").unwrap().as_deref(),
        Some("inv-9")
    );
    let unit_dir = home.path().join(".config/systemd/user");
    assert!(unit_dir.join("pg-x.timer").exists() && unit_dir.join("pg-x.service").exists());
    let calls = sys.calls.borrow();
    assert!(
        calls.contains(&"systemctl --user enable pg-x.timer".to_string()),
        "{calls:?}"
    );
    assert!(
        calls.contains(&"systemctl --user restart pg-x.service".to_string()),
        "{calls:?}"
    );
    drop(calls);
    let stale = FakeSys {
        home: home.path().into(),
        calls: RefCell::default(),
        landed: false,
    };
    let step = Step {
        sys: &stale,
        repo: repo.path().into(),
        rev: "r1".into(),
    };
    assert!(step
        .install("pg-x.service")
        .unwrap_err()
        .contains("does not contain the landed revision"));
    assert!(!stale
        .calls
        .borrow()
        .iter()
        .any(|c| c.starts_with("systemctl")));
}

#[test]
fn systemctl_show_reads_as_a_first_run() {
    let run = parse_show("LoadState=loaded\nActiveState=inactive\nResult=success\nExecMainExitTimestamp=Sun 2026-11-01 12:01:00 UTC\nInvocationID=abc\nType=oneshot\n");
    assert_eq!(
        (
            run.is_active.as_str(),
            run.oneshot,
            run.invocation.as_deref()
        ),
        ("inactive", true, Some("abc"))
    );
    assert_eq!(
        parse_show("LoadState=not-found\nActiveState=inactive\n").is_active,
        ABSENT
    );
    assert_eq!(
        parse_show("LoadState=loaded\nActiveState=active\nInvocationID=\n").invocation,
        None
    );
}

#[test]
fn a_train_deploy_plans_the_landed_range_and_binds_plan_and_state_to_the_merge() {
    use super::deploy_train::step_flags;
    let flags: std::collections::BTreeMap<String, String> = [
        ("base", "b0"),
        ("merge", "m1"),
        ("train-head", "h1"),
        ("state-dir", "/s"),
    ]
    .map(|(k, v)| (k.to_owned(), v.to_owned()))
    .into();
    let env = |k: &str| (k == "PG_LAND_DEPLOY_HOSTS").then(|| "/hosts.json".to_owned());
    let (plan, run) = step_flags(&flags, true, &env).unwrap();
    assert_eq!(
        (plan["base"].as_str(), plan["head"].as_str()),
        ("m1^1", "m1")
    );
    assert_eq!(plan["plan-out"], "/s/m1.plan.json");
    assert_eq!(run["plan"], "/s/m1.plan.json");
    assert_eq!(run["state"], "/s/m1.state.json");
    assert_eq!(
        (run["mode"].as_str(), run["hosts"].as_str()),
        ("apply", "/hosts.json")
    );
    assert!(!run.contains_key("row-cmd"));
    let mut ff = flags.clone();
    ff.insert("merge".into(), "h1".into());
    let (plan, run) = step_flags(&ff, false, &|_| None).unwrap();
    assert_eq!(
        (plan["base"].as_str(), run["mode"].as_str()),
        ("b0", "dry-run")
    );
}
