use super::install::{install_units, Report, Target};
use super::systemd::fixture::Fixture;
use super::*;
use std::fs;

const MANIFEST: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["keeper"]
env_files = ["a.env"]
first_run = "ok"

[[unit]]
name = "pg-t"
files = ["pg-t@.service"]
hosts = ["all"]
env_files = ["x/INSTANCE.env"]
first_run = "ok"

[[unit]]
name = "pg-sys"
files = ["pg-sys.service", "pg-sys.timer"]
scope = "system"
hosts = ["all"]
first_run = "ok"

[[unit]]
name = "pg-w"
files = ["pg-w.service"]
hosts = ["worker"]
first_run = "ok"
"#;

struct World {
    _dir: tempfile::TempDir,
    repo: std::path::PathBuf,
    units: std::path::PathBuf,
    config: std::path::PathBuf,
}

fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path().join("repo");
    fs::create_dir_all(repo.join("deploy/systemd")).unwrap();
    for file in [
        "pg-a.service",
        "pg-a.timer",
        "pg-t@.service",
        "pg-sys.service",
        "pg-sys.timer",
        "pg-w.service",
    ] {
        fs::write(
            repo.join("deploy/systemd").join(file),
            format!("[Unit]\n# {file}\n"),
        )
        .unwrap();
    }
    let units = dir.path().join("units");
    let config = dir.path().join("config");
    fs::create_dir_all(&config).unwrap();
    World {
        _dir: dir,
        repo,
        units,
        config,
    }
}

fn run(w: &World, roles: &[&str], sysctl: &Fixture) -> Report {
    let manifest = Manifest::parse(MANIFEST).unwrap();
    let roles = roles.iter().map(|r| (*r).to_owned()).collect();
    let target = Target {
        repo: &w.repo,
        unit_dir: &w.units,
        config_dir: &w.config,
        scope: Scope::User,
    };
    install_units(&manifest, &roles, &target, sysctl)
}

#[test]
fn a_family_is_copied_reloaded_and_enabled_when_not_active() {
    let w = world();
    fs::write(w.config.join("a.env"), "").unwrap();
    let mut sysctl = Fixture::default();
    sysctl.answer("--user is-enabled pg-a.timer", 1, "disabled\n");
    sysctl.answer("--user is-active pg-a.timer", 3, "inactive\n");
    let report = run(&w, &["keeper"], &sysctl);
    assert!(!report.pending, "{report:?}");
    assert!(w.units.join("pg-a.timer").is_file() && w.units.join("pg-a.service").is_file());
    let calls = sysctl.calls.borrow().join(" | ");
    assert!(
        calls.contains("daemon-reload") && calls.contains("enable --now pg-a.timer"),
        "{calls}"
    );
    assert!(
        report.lines.iter().any(|l| l.starts_with("ENABLED pg-a")),
        "{report:?}"
    );
}

#[test]
fn a_second_run_changes_nothing_when_the_unit_is_already_live() {
    let w = world();
    fs::write(w.config.join("a.env"), "").unwrap();
    let mut sysctl = Fixture::default();
    sysctl.answer("--user is-enabled pg-a.timer", 0, "enabled\n");
    sysctl.answer("--user is-active pg-a.timer", 0, "active\n");
    run(&w, &["keeper"], &sysctl);
    let again = Fixture {
        answers: sysctl.answers.clone(),
        ..Fixture::default()
    };
    let report = run(&w, &["keeper"], &again);
    assert!(
        report.lines.iter().any(|l| l.starts_with("SAME pg-a")),
        "{report:?}"
    );
    let calls = again.calls.borrow().join(" | ");
    assert!(
        !calls.contains("daemon-reload") && !calls.contains("enable --now"),
        "{calls}"
    );
}

#[test]
fn a_family_without_its_env_file_is_reported_and_not_enabled() {
    let w = world();
    let sysctl = Fixture::default();
    let report = run(&w, &["keeper"], &sysctl);
    assert!(report.pending);
    assert!(
        report.lines.iter().any(|l| l == "NEEDS-ENV pg-a: a.env"),
        "{report:?}"
    );
    assert!(
        !sysctl.calls.borrow().iter().any(|c| c.contains("enable")),
        "{:?}",
        sysctl.calls
    );
}

#[test]
fn templates_are_installed_but_not_enabled_and_other_scopes_and_roles_are_left_alone() {
    let w = world();
    let sysctl = Fixture::default();
    let report = run(&w, &["worker"], &sysctl);
    assert!(w.units.join("pg-t@.service").is_file());
    assert!(
        !w.units.join("pg-sys.timer").exists(),
        "a system unit is not written by a user run"
    );
    assert!(
        !w.units.join("pg-a.timer").exists(),
        "a keeper unit is not installed on a worker"
    );
    assert!(
        report.lines.iter().any(|l| l.starts_with("SKIP pg-sys")),
        "{report:?}"
    );
    assert!(
        report.lines.iter().any(|l| l.starts_with("OK pg-t")),
        "{report:?}"
    );
    assert!(
        sysctl.calls.borrow().iter().all(|c| !c.contains("pg-t@")),
        "{:?}",
        sysctl.calls
    );
}

#[test]
fn a_failed_enable_leaves_the_run_pending() {
    let w = world();
    fs::write(w.config.join("a.env"), "").unwrap();
    let mut sysctl = Fixture::default();
    sysctl.answer("--user enable --now pg-a.timer", 1, "");
    let report = run(&w, &["keeper"], &sysctl);
    assert!(
        report.pending && report.lines.iter().any(|l| l.starts_with("FAIL pg-a")),
        "{report:?}"
    );
}
