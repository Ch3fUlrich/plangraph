#![cfg(unix)]
//! `plangraph deploy-status` end to end with a fake `systemctl` on PATH: no real systemd is read.
use std::{fs, process::Command};

const MANIFEST: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["keeper"]
first_run = "start pg-a.service"
"#;

fn run(systemctl_script: &str) -> (std::process::Output, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path().join("repo");
    fs::create_dir_all(repo.join("deploy")).unwrap();
    fs::write(repo.join("deploy/manifest.toml"), MANIFEST).unwrap();
    let tools = dir.path().join("tools");
    fs::create_dir_all(&tools).unwrap();
    pg_worker_core::executable_test_support::write_executable(
        tools.join("systemctl"),
        systemctl_script,
    );
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env_clear()
        .env("HOME", dir.path())
        .env("PATH", format!("{}:/usr/bin:/bin", tools.display()))
        .env("PG_HOST_ROLES", "keeper")
        .args(["deploy-status", "--repo"])
        .arg(&repo)
        .output()
        .unwrap();
    (output, dir)
}

#[test]
fn a_unit_systemd_does_not_know_is_listed_not_live_and_fails_the_run() {
    let (output, dir) = run("#!/bin/sh\necho LoadState=not-found\necho ActiveState=inactive\n");
    let text = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(
        text.contains("deploy-entry\tunit\tpg-a\tnot-installed\t"),
        "{text}"
    );
    assert!(
        text.contains("start pg-a.service"),
        "the first-run check is shown: {text}"
    );
    assert!(text.contains("deploy-not-live\t1"), "{text}");
    let ledgers = dir.path().join(".local/state/plangraph/deploy-live");
    assert!(
        fs::read_dir(&ledgers).unwrap().count() == 1,
        "the ledger is written under {}",
        ledgers.display()
    );
}

#[test]
fn a_live_unit_gives_a_clean_run() {
    let script = "#!/bin/sh\necho LoadState=loaded\necho ActiveState=active\necho UnitFileState=enabled\necho Result=success\necho ExecMainStartTimestamp=@100\necho ExecMainStatus=0\necho ActiveEnterTimestamp=@100\n";
    let (output, _dir) = run(script);
    let text = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert!(
        text.contains("deploy-entry\tunit\tpg-a\tlive\t-\t"),
        "{text}"
    );
    assert!(text.contains("deploy-not-live\t0"), "{text}");
}

#[test]
fn an_active_timer_whose_service_never_ran_is_not_live() {
    let script = "#!/bin/sh\necho LoadState=loaded\necho ActiveState=active\necho UnitFileState=enabled\necho Result=success\n";
    let (output, _dir) = run(script);
    let text = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(
        text.contains("never-run(pg-a.service has not run since it was installed)"),
        "{text}"
    );
}
