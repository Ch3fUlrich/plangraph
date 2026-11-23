#![cfg(target_os = "linux")]

use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn install(home: &Path, family: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .args(["--install-env", family])
        .output()
        .unwrap()
}

#[test]
fn disk_hygiene_environment_matches_unit_and_preserves_existing_configuration() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("fixture home");
    let env = home.join(".config/plangraph/disk-hygiene.env");
    let output = install(&home, "disk-hygiene");
    assert!(output.status.success(), "{output:?}");
    let text = fs::read_to_string(&env).unwrap();
    for (key, path) in [
        ("PG_REPO", "code/plangraph"),
        ("PG_WORKTREES", "code/plangraph/.claude/worktrees"),
        ("PG_PINS", ".config/plangraph/pins.json"),
    ] {
        assert!(
            text.contains(&format!("{key}=\"{}\"\n", home.join(path).display())),
            "{text}"
        );
    }
    let binary = home.join(".local/bin/pg-disk-hygiene");
    fs::create_dir_all(binary.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink("/bin/true", binary).unwrap();
    let unit = include_str!("../../../deploy/systemd/pg-disk-hygiene.service");
    if Path::new("/usr/bin/systemd-analyze").is_file() {
        let service = temp.path().join("pg-disk-hygiene.service");
        let unit = unit.replace(
            "%h/.local/bin/pg-disk-hygiene",
            &format!("\"{}\"", home.join(".local/bin/pg-disk-hygiene").display()),
        );
        fs::write(&service, unit).unwrap();
        let verify = Command::new("/usr/bin/systemd-analyze")
            .arg("verify")
            .arg(service)
            .output()
            .unwrap();
        assert!(verify.status.success(), "{verify:?}");
    }
    fs::write(&env, "PG_REPO=existing\n").unwrap();
    assert!(install(&home, "disk-hygiene").status.success());
    assert_eq!(fs::read_to_string(env).unwrap(), "PG_REPO=existing\n");
}

#[test]
fn keeper_backup_installs_only_empty_connection_keys_with_private_mode() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let output = install(home, "keeper-backup");
    assert!(output.status.success(), "{output:?}");
    let env = home.join(".config/plangraph/keeper-backup.env");
    let text = fs::read_to_string(&env).unwrap();
    let docker = include_str!("../../pg-keeper-backup/src/docker.rs");
    let keys = docker
        .split_once("pub const PG_ENV:")
        .unwrap()
        .1
        .split_once("= [")
        .unwrap()
        .1
        .split_once("];")
        .unwrap()
        .0;
    let expected: Vec<_> = keys
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.trim_end_matches(',').trim_matches('"'))
        .collect();
    let entries: Vec<_> = text
        .lines()
        .filter(|line| !line.starts_with('#'))
        .map(|line| line.split_once('=').unwrap())
        .collect();
    assert_eq!(
        entries,
        expected
            .into_iter()
            .map(|key| (key, ""))
            .collect::<Vec<_>>()
    );
    assert_eq!(
        fs::metadata(&env).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::write(&env, "# existing operator file\n").unwrap();
    assert!(install(home, "keeper-backup").status.success());
    assert_eq!(
        fs::read_to_string(env).unwrap(),
        "# existing operator file\n"
    );
}

#[test]
fn quota_alarms_discover_the_store_and_report_missing_candidates() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let db = home.join(".omniroute/storage.sqlite");
    let alternate = home.join(".local/share/omniroute/storage.sqlite");
    let target = home.join(".config/plangraph/quota-alarms.env");
    let missing = install(home, "quota-alarms");
    assert!(!missing.status.success());
    let message = String::from_utf8_lossy(&missing.stderr);
    for path in [&db, &alternate] {
        assert!(message.contains(path.to_str().unwrap()), "{message}");
    }
    assert!(!target.exists());

    fixture_quota_db(&db);
    let output = install(home, "quota-alarms");
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        fs::read_to_string(target).unwrap(),
        format!("QUOTA_SNAPSHOT_DB=\"{}\"\n", db.display())
    );
}

fn fixture_quota_db(path: &Path) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    rusqlite::Connection::open(path)
        .unwrap()
        .execute_batch(
            "CREATE TABLE quota_snapshots(id INTEGER, provider TEXT, connection_id TEXT,
            window_key TEXT, remaining_percentage REAL, is_exhausted INTEGER,
            next_reset_at TEXT, created_at TEXT)",
        )
        .unwrap();
}

#[test]
fn quota_alarms_uses_gateway_mount_before_a_cli_store() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    fixture_quota_db(&home.join(".omniroute/storage.sqlite"));
    let mount = home.join("gateway mount");
    let database = mount.join("storage.sqlite");
    fixture_quota_db(&database);
    let docker = home.join("docker");
    fs::write(
        &docker,
        "#!/bin/sh\n[ \"$1\" = inspect ] || exit 1\nprintf '%s\\n' \"$FIXTURE_MOUNTS\"\n",
    )
    .unwrap();
    fs::set_permissions(docker, fs::Permissions::from_mode(0o755)).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .env("PATH", home)
        .env("QUOTA_GATEWAY_CONTAINER", "fixture")
        .env(
            "FIXTURE_MOUNTS",
            serde_json::json!([{"Source": mount, "Destination": "/app/data"}]).to_string(),
        )
        .args(["--install-env", "quota-alarms"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        fs::read_to_string(home.join(".config/plangraph/quota-alarms.env")).unwrap(),
        format!("QUOTA_SNAPSHOT_DB=\"{}\"\n", database.display())
    );
}

#[test]
fn state_shadow_stages_both_instances_and_registers_measurement_once() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let settings = [home.join("plan-order.json"), home.join("routing.json")];
    let unit = include_str!("../../../deploy/systemd/plangraph-state-shadow@.service");
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin).unwrap();
    for name in ["plangraph", "turn-budget-hook"] {
        std::os::unix::fs::symlink("/bin/true", bin.join(name)).unwrap();
    }
    for path in &settings {
        fs::write(path, r#"{"permissions":{"defaultMode":"default"},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"existing"}]}]}}"#).unwrap();
    }
    let run = || {
        Command::new(env!("CARGO_BIN_EXE_host-sync"))
            .env_clear()
            .env("HOME", home)
            .args(["--install-env", "state-shadow"])
            .args(&settings)
            .output()
            .unwrap()
    };
    for _ in 0..2 {
        let output = run();
        assert!(output.status.success(), "{output:?}");
    }
    let config = home.join(".config/plangraph/state-shadow");
    for (role, settings) in ["plan-order", "routing"].into_iter().zip(settings) {
        let content = fs::read_to_string(config.join(format!("{role}.env"))).unwrap();
        for (key, suffix) in [
            ("PG_TOKEN_FILE", "token"),
            ("PLANGRAPH_STATE_CONFIG", "json"),
        ] {
            assert!(content.contains(&format!(
                "{key}=\"{}\"\n",
                config.join(format!("{role}.{suffix}")).display()
            )));
        }
        let props: serde_json::Value =
            serde_json::from_slice(&fs::read(settings).unwrap()).unwrap();
        assert_eq!(props["permissions"]["defaultMode"], "default");
        assert_eq!(props["hooks"]["Stop"][0]["hooks"][0]["command"], "existing");
        for event in [
            "UserPromptSubmit",
            "Notification",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ] {
            let groups = props["hooks"][event].as_array().unwrap();
            let matching: Vec<_> = groups
                .iter()
                .flat_map(|group| group["hooks"].as_array().unwrap())
                .filter(|hook| {
                    hook["command"] == "\"$HOME/.local/bin/turn-budget-hook\" --measure-only"
                })
                .collect();
            assert_eq!(matching.len(), 1, "{event}: {groups:?}");
        }
        if Path::new("/usr/bin/systemd-analyze").is_file() {
            let service = temp
                .path()
                .join(format!("plangraph-state-shadow@{role}.service"));
            fs::write(&service, unit.replace("%h", home.to_str().unwrap())).unwrap();
            let verify = Command::new("/usr/bin/systemd-analyze")
                .arg("verify")
                .arg(service)
                .output()
                .unwrap();
            assert!(verify.status.success(), "{verify:?}");
        }
    }
}
