use super::build_tests::{g, Fx};
use super::deploy::{DeployPlan, Target};
use super::deploy_cmd::*;
use super::deploy_post::*;
use std::collections::{BTreeMap, BTreeSet};

fn target(family: &str, host: &str, items: &[&str]) -> Target {
    Target {
        family: family.into(),
        host: host.into(),
        parts: vec![1],
        items: items.iter().map(|s| s.to_string()).collect(),
    }
}

#[cfg(unix)]
fn sh(script: &str) -> Vec<String> {
    vec!["sh".into(), "-c".into(), script.into()]
}

#[cfg(unix)]
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

#[cfg(unix)]
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

#[cfg(unix)]
fn script(dir: &std::path::Path, name: &str, body: &str) -> String {
    pg_worker_core::executable_test_support::write_script(
        dir,
        name,
        &format!("#!/bin/sh\n{body}\n"),
    )
    .display()
    .to_string()
}

/// A commit on main made at `epoch` with `files` written and `message`; returns its sha.
fn commit_at(fx: &Fx, epoch: i64, files: &[(&str, &str)], message: &str) -> String {
    let p = fx.path();
    for (name, text) in files {
        let path = p.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    g(p, &["add", "-A"]);
    let date = format!("{epoch} +0000");
    let o = std::process::Command::new("git")
        .current_dir(p)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@example.invalid")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@example.invalid")
        .env("GIT_COMMITTER_DATE", &date)
        .env("GIT_AUTHOR_DATE", &date)
        .args(["commit", "-q", "-m", message])
        .output()
        .unwrap();
    assert!(o.status.success());
    g(p, &["rev-parse", "HEAD"])
}

/// base -> train (ships `a.service` for demo:B, 2025) -> landed (a later commit on main, 2026-01-01).
struct Landed {
    base: String,
    train: String,
    landed: String,
}

fn landed(fx: &Fx) -> Landed {
    let base = fx.base();
    let train = commit_at(
        fx,
        1_700_000_000,
        &[("deploy/systemd/a.service", "[Service]\n")],
        "unit\n\nPlan-Item: demo:B",
    );
    let landed = commit_at(fx, 1_767_225_600, &[("landed.txt", "x\n")], "landed");
    Landed {
        base,
        train,
        landed,
    }
}

fn plan_of(l: &Landed) -> DeployPlan {
    DeployPlan {
        targets: vec![target("a.service", "alpha", &["demo:B"])],
        blockers: vec![],
        train_sha: l.train.clone(),
        base: l.base.clone(),
    }
}

const GO: &str = "deploy: a.service@alpha";

fn bind_it(
    fx: &Fx,
    plan: &DeployPlan,
    go: &str,
    head: &str,
) -> Result<(Vec<Target>, Gate), String> {
    bind(plan, go, fx.path(), head, "main", &plan.digest())
}

#[test]
fn bind_accepts_a_plan_the_landed_train_really_ships() {
    let fx = Fx::new();
    let l = landed(&fx);
    let (targets, gate) = bind_it(&fx, &plan_of(&l), GO, &l.landed).unwrap();
    assert_eq!(targets.len(), 1);
    assert_eq!(gate.fresh_after, 1_767_225_600);
}

#[test]
fn bind_refuses_a_foreign_go_a_foreign_train_a_head_off_main_and_an_edited_plan() {
    let fx = Fx::new();
    let l = landed(&fx);
    let off_main = fx.commit(&l.train, "other.txt", "x\n");
    let plan = plan_of(&l);
    let e = bind_it(&fx, &plan, "deploy: a.service@beta", &l.landed).unwrap_err();
    assert!(e.contains("is not this plan's"), "{e}");
    let mut nameless = plan_of(&l);
    nameless.train_sha.clear();
    assert!(bind_it(&fx, &nameless, GO, &l.landed)
        .unwrap_err()
        .contains("names no train"));
    // A train that is not merged into the landed head (the landed head predates it).
    let e = bind_it(&fx, &plan, GO, &l.base).unwrap_err();
    assert!(e.contains("is not merged into landed head"), "{e}");
    let e = bind_it(&fx, &plan, GO, &off_main).unwrap_err();
    assert!(e.contains("is not on main"), "{e}");
    // Item keys are part of the approved digest: an edited plan keeps its GO line but not its digest.
    let mut edited = plan_of(&l);
    edited.targets[0].items = ["demo:Z".to_string()].into();
    let e = bind(&edited, GO, fx.path(), &l.landed, "main", &plan.digest()).unwrap_err();
    assert!(e.contains("approved plan digest"), "{e}");
}

#[test]
fn bind_refuses_a_target_the_landed_train_does_not_ship() {
    let fx = Fx::new();
    let l = landed(&fx);
    // A unit absent from the train, with a matching plan, GO line and digest.
    let mut ghost = plan_of(&l);
    ghost.targets[0].family = "ghost.service".into();
    let e = bind_it(&fx, &ghost, "deploy: ghost.service@alpha", &l.landed).unwrap_err();
    assert!(e.contains("is not shipped by train"), "{e}");
    // An item key no commit of the train carries.
    let mut foreign = plan_of(&l);
    foreign.targets[0].items = ["demo:Z".to_string()].into();
    let e = bind_it(&fx, &foreign, GO, &l.landed).unwrap_err();
    assert!(e.contains("is not a Plan-Item of train"), "{e}");
    // A unit the landed tree no longer has.
    g(fx.path(), &["rm", "-q", "deploy/systemd/a.service"]);
    let gone = commit_at(&fx, 1_767_225_700, &[], "remove unit");
    let e = bind_it(&fx, &plan_of(&l), GO, &gone).unwrap_err();
    assert!(e.contains("absent from the landed tree"), "{e}");
}

#[test]
fn bind_withholds_a_preflight_refused_timer_in_the_gate() {
    let fx = Fx::new();
    let l = landed(&fx);
    let mut plan = plan_of(&l);
    plan.blockers.push(super::deploy::Blocker {
        key: "plangraph:LE-DEPLOY-BLOCKED-A-TIMER-ALPHA".into(),
        family: "a.timer".into(),
        host: Some("alpha".into()),
        parts: vec![1],
        reasons: vec!["missing env X".into()],
    });
    let (_, gate) = bind_it(&fx, &plan, GO, &l.landed).unwrap();
    assert_eq!(
        gate.excluded,
        BTreeSet::from([("a.timer".to_string(), "alpha".to_string())])
    );
}

#[cfg(unix)]
#[test]
fn deploy_apply_end_to_end_writes_rows_then_resumes_and_a_failure_blocks_the_rerun() {
    let fx = Fx::new();
    let l = landed(&fx);
    let plan = plan_of(&l);
    let dir = tempfile::tempdir().unwrap();
    let d = dir.path();
    std::fs::write(d.join("plan.json"), serde_json::to_vec(&plan).unwrap()).unwrap();
    let rows_dir = d.join("rows");
    std::fs::create_dir(&rows_dir).unwrap();
    let install = script(
        d,
        "install.sh",
        r#"if [ "$1" = read ]; then
  if [ "$3" = a.timer ]; then echo '{"is_active":"active"}'; else echo '{"is_active":"inactive","result":"success","exec_main_exit_timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","oneshot":true}'; fi
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
        ("go", GO.to_string()),
        ("plan-digest", plan.digest()),
        ("repo", fx.path().display().to_string()),
        ("landed-head", l.landed.clone()),
        ("main-ref", "main".to_string()),
        ("state", d.join("state.json").display().to_string()),
        ("install-cmd", install),
        ("row-cmd", row_cmd),
    ] {
        flags.insert(k.into(), v);
    }
    let lines = run_cli(&flags).unwrap();
    assert_eq!(lines, ["deployed a.service@alpha"]);
    let written = std::fs::read_to_string(rows_dir.join("demo:B")).unwrap();
    assert_eq!(written.matches("\"unit\"").count(), 2, "{written}");
    // A rerun resumes: the target is done and nothing is written again.
    assert_eq!(run_cli(&flags).unwrap(), ["already done a.service@alpha"]);
    assert_eq!(
        std::fs::read_to_string(rows_dir.join("demo:B")).unwrap(),
        written
    );
    // A failed first run is an error, saved in the state, and blocks the next run.
    std::fs::remove_file(d.join("state.json")).unwrap();
    flags.insert("install-cmd".into(), script(d, "bad.sh", "exit 1"));
    let e = run_cli(&flags).unwrap_err();
    assert!(e.contains("stop: a.service@alpha"), "{e}");
    let blocked = run_cli(&flags).unwrap_err();
    assert!(blocked.contains("earlier first run failed"), "{blocked}");
    // A state saved by another train is refused.
    let state = PostState {
        train: "other".into(),
        ..PostState::default()
    };
    std::fs::write(d.join("state.json"), serde_json::to_vec(&state).unwrap()).unwrap();
    let e = run_cli(&flags).unwrap_err();
    assert!(e.contains("belongs to train other"), "{e}");
}
