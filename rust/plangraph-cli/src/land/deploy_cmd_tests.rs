use super::build_tests::{g, Fx};
use super::deploy::{DeployPlan, Target};
use super::deploy_bind::Approved;
use super::deploy_cmd::*;
use super::deploy_post::*;
use super::go_verify::Evidence;
use super::go_verify_tests::green;
use std::collections::BTreeMap;

pub(super) fn target(family: &str, host: &str, items: &[&str]) -> Target {
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
    assert_eq!(inst.install("alpha", "a.service").unwrap(), None);
    let printing = CommandInstaller {
        argv: sh("echo inv-9"),
    };
    assert_eq!(
        printing.install("alpha", "a.service").unwrap().as_deref(),
        Some("inv-9")
    );
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
pub(super) fn commit_at(fx: &Fx, epoch: i64, files: &[(&str, &str)], message: &str) -> String {
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
pub(super) struct Landed {
    pub(super) base: String,
    pub(super) train: String,
    pub(super) landed: String,
}

pub(super) fn landed(fx: &Fx) -> Landed {
    let base = fx.base();
    commit_at(
        fx,
        1_700_000_000,
        &[("deploy/systemd/a.service", "[Service]\n")],
        "unit\n\nPlan-Item: demo:B",
    );
    let train = commit_at(
        fx,
        1_700_000_100,
        &[("docs/c.md", "c\n")],
        "docs\n\nPlan-Item: demo:C",
    );
    let landed = commit_at(fx, 1_767_225_600, &[("landed.txt", "x\n")], "landed");
    Landed {
        base,
        train,
        landed,
    }
}

pub(super) fn plan_of(l: &Landed) -> DeployPlan {
    DeployPlan {
        targets: vec![target("a.service", "alpha", &["demo:B"])],
        blockers: vec![],
        train_sha: l.train.clone(),
        base: l.base.clone(),
    }
}

pub(super) const GO: &str = "deploy: a.service@alpha";

pub(super) fn approved(l: &Landed) -> Approved {
    Approved {
        train_sha: l.train.clone(),
        base: l.base.clone(),
    }
}

/// Green GO evidence of the fixture train.
fn evidence_of(l: &Landed) -> Evidence {
    let mut e = green();
    e.base = l.base.clone();
    e.head = l.train.clone();
    e.observed.pr_head = l.train.clone();
    e.observed.pr_base = l.base.clone();
    e.observed.main_tip = l.base.clone();
    e.replay.base = l.base.clone();
    e.replay.train_sha = l.train.clone();
    e.ci.head = l.train.clone();
    e.approvals[0].head = l.train.clone();
    if let Some(d) = e.delta.as_mut() {
        d.head = l.train.clone();
    }
    e
}

#[cfg(unix)]
#[test]
fn deploy_apply_end_to_end_writes_rows_then_resumes_and_a_failure_blocks_the_rerun() {
    let fx = Fx::new();
    let l = landed(&fx);
    let plan = plan_of(&l);
    let dir = tempfile::tempdir().unwrap();
    let d = dir.path();
    std::fs::write(
        d.join("go.json"),
        serde_json::to_vec(&evidence_of(&l)).unwrap(),
    )
    .unwrap();
    std::fs::write(d.join("plan.json"), serde_json::to_vec(&plan).unwrap()).unwrap();
    let rows_dir = d.join("rows");
    std::fs::create_dir(&rows_dir).unwrap();
    let marker = d.join("installed");
    let install = script(
        d,
        "install.sh",
        &format!(
            r#"M={m}
[ "$1" = install ] && touch "$M" && [ "$3" = a.service ] && echo inv-1
if [ "$1" = read ]; then
  if [ "$3" = a.timer ]; then echo '{{"is_active":"active"}}'
  else
    [ -e "$M" ] || {{ echo '{{"is_active":"absent"}}'; exit 0; }}
    echo '{{"is_active":"inactive","result":"success","exec_main_exit_timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","oneshot":true,"invocation":"inv-1"}}'
  fi
fi
exit 0"#,
            m = marker.display()
        ),
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
        ("evidence", d.join("go.json").display().to_string()),
        ("repo", fx.path().display().to_string()),
        ("landed-head", l.landed.clone()),
        ("main-ref", "main".to_string()),
        ("state", d.join("state.json").display().to_string()),
        ("install-cmd", install),
        ("row-cmd", row_cmd),
    ] {
        flags.insert(k.into(), v);
    }
    // Evidence of another train, or one the verifier refuses, installs nothing.
    let mut other = evidence_of(&l);
    other.head = l.base.clone();
    other.replay.train_sha = l.base.clone();
    other.observed.pr_head = l.base.clone();
    other.ci.head = l.base.clone();
    other.approvals[0].head = l.base.clone();
    other.delta.as_mut().unwrap().head = l.base.clone();
    let mut foreign = flags.clone();
    std::fs::write(d.join("other.json"), serde_json::to_vec(&other).unwrap()).unwrap();
    foreign.insert(
        "evidence".into(),
        d.join("other.json").display().to_string(),
    );
    assert!(run_cli(&foreign)
        .unwrap_err()
        .contains("the approved GO is for"));
    let mut red = evidence_of(&l);
    red.ci.jobs.clear();
    std::fs::write(d.join("red.json"), serde_json::to_vec(&red).unwrap()).unwrap();
    foreign.insert("evidence".into(), d.join("red.json").display().to_string());
    assert!(run_cli(&foreign)
        .unwrap_err()
        .contains("the GO evidence is refused"));
    assert!(!d.join("state.json").exists());
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
