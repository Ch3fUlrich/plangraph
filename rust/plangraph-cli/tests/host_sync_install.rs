#![cfg(unix)]
//! `host-sync --install` end to end with fake `systemctl` and `ssh` on PATH: nothing real is touched.
use std::{fs, path::Path, process::Command};

const MANIFEST: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["all"]
first_run = "start pg-a.service"
"#;

fn script(dir: &Path, name: &str, body: &str) {
    fs::create_dir_all(dir).unwrap();
    pg_worker_core::executable_test_support::write_executable(dir.join(name), body);
}

fn host_sync(home: &Path, tools: &Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .env("PATH", format!("{}:/usr/bin:/bin", tools.display()))
        .env("PG_HOST_ROLES", "keeper")
        .args(args)
        .output()
        .unwrap()
}

fn fixture() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path().join("repo");
    fs::create_dir_all(repo.join("deploy/systemd")).unwrap();
    fs::write(repo.join("deploy/manifest.toml"), MANIFEST).unwrap();
    for file in ["pg-a.service", "pg-a.timer"] {
        fs::write(repo.join("deploy/systemd").join(file), "[Unit]\n").unwrap();
    }
    (dir, repo)
}

#[test]
fn check_lists_what_is_missing_and_exits_one() {
    let (dir, repo) = fixture();
    let tools = dir.path().join("tools");
    script(&tools, "systemctl", "#!/bin/sh\necho LoadState=not-found\n");
    let out = host_sync(
        dir.path(),
        &tools,
        &["--install", "--check", "--repo", repo.to_str().unwrap()],
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert_eq!(out.status.code(), Some(1), "{out:?}");
    assert!(text.contains("plan\tunit\tpg-a\tpg-a.service missing, pg-a.timer missing\tinstall unit files and enable"), "{text}");
    assert!(
        !dir.path().join(".config/systemd/user").exists(),
        "--check installs nothing"
    );
}

#[test]
fn the_fleet_form_runs_every_registry_host_over_ssh_and_prints_one_table() {
    let (dir, _repo) = fixture();
    let tools = dir.path().join("tools");
    // The fake ssh answers by the target it is given (argument 5 after the -o options).
    script(
        &tools,
        "ssh",
        "#!/bin/sh\ncase \"$5\" in\n  good) printf 'nothing to do\\t3 entries live\\npending\\t0\\nledger\\t{\"written_at\":1}\\n';;\n  bad) printf 'plan\\tunit\\tpg-a\\tnot-installed\\tinstall unit files and enable\\npending\\t1\\nledger\\t{\"written_at\":1}\\n'; exit 1;;\n  *) echo 'ssh: Connection refused' >&2; exit 255;;\nesac\n",
    );
    let config = dir.path().join(".config/plangraph");
    fs::create_dir_all(&config).unwrap();
    fs::write(
        config.join("hosts.json"),
        r#"{"version":1,"self":"c","hosts":[
          {"id":"good","transport":"ssh","ssh_target":"good"},
          {"id":"bad","transport":"ssh","ssh_target":"bad"},
          {"id":"gone","transport":"ssh","ssh_target":"gone"}]}"#,
    )
    .unwrap();
    let out = host_sync(dir.path(), &tools, &["--install", "--fleet", "--check"]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert_eq!(out.status.code(), Some(1), "{out:?}");
    assert!(
        text.contains("good\tOK\tcomplete") && text.contains("bad\tPENDING"),
        "{text}"
    );
    assert!(
        text.contains("gone\tFAIL\tunreachable: ssh: Connection refused"),
        "{text}"
    );
    assert!(
        dir.path()
            .join(".local/state/plangraph/deploy-live/good.json")
            .is_file(),
        "ledger row on the center"
    );
}
