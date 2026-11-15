use pg_worker_core::host_link::observe;
use serde_json::Value;
use std::fs;

fn events(root: &std::path::Path) -> Vec<Value> {
    let outbox = root.join("watchdog/outbox");
    if !outbox.exists() {
        return vec![];
    }
    fs::read_dir(outbox)
        .unwrap()
        .map(|p| serde_json::from_slice(&fs::read(p.unwrap().path()).unwrap()).unwrap())
        .collect()
}

#[test]
fn peer_block_alerts_only_after_five_minutes_and_survives_restart() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    observe(root, "build-a", "coordinator", 100, false).unwrap();
    observe(root, "build-a", "coordinator", 400, false).unwrap();
    assert!(events(root).is_empty());
    // Each call reloads state, just as independent scheduler invocations do.
    observe(root, "build-a", "coordinator", 401, false).unwrap();
    let alerts = events(root);
    assert_eq!(alerts.len(), 1);
    assert_eq!(alerts[0]["kind"], "alert");
    assert_eq!(alerts[0]["recipient"], "routing");
    assert_eq!(alerts[0]["rule"], "host-link-down");
    assert_eq!(alerts[0]["host"], "build-a");
    assert_eq!(alerts[0]["peer"], "coordinator");
    assert_eq!(alerts[0]["down_since"], 100);
    // Delivery removes the outbox file; a continuing outage must not recreate it.
    for entry in fs::read_dir(root.join("watchdog/outbox")).unwrap() {
        fs::remove_file(entry.unwrap().path()).unwrap();
    }
    observe(root, "build-a", "coordinator", 500, false).unwrap();
    assert!(events(root).is_empty());
    observe(root, "build-a", "coordinator", 501, true).unwrap();
    observe(root, "build-a", "coordinator", 502, false).unwrap();
    observe(root, "build-a", "coordinator", 803, false).unwrap();
    assert_eq!(events(root)[0]["down_since"], 502);
}

#[test]
fn recovery_does_not_erase_pending_alerts_and_hosts_are_independent() {
    let dir = tempfile::tempdir().unwrap();
    for host in ["build-a", "build-b"] {
        observe(dir.path(), host, "coordinator", 10, false).unwrap();
    }
    observe(dir.path(), "build-a", "coordinator", 300, true).unwrap();
    observe(dir.path(), "build-b", "coordinator", 311, false).unwrap();
    observe(dir.path(), "build-b", "coordinator", 312, true).unwrap();
    assert_eq!(events(dir.path()).len(), 1);
    assert_eq!(events(dir.path())[0]["host"], "build-b");
}

#[test]
fn failed_queue_write_is_retried_without_resetting_outage() {
    let dir = tempfile::tempdir().unwrap();
    observe(dir.path(), "build-a", "coordinator", 10, false).unwrap();
    fs::write(dir.path().join("watchdog/outbox"), "blocked").unwrap();
    assert!(observe(dir.path(), "build-a", "coordinator", 311, false).is_err());
    fs::remove_file(dir.path().join("watchdog/outbox")).unwrap();
    observe(dir.path(), "build-a", "coordinator", 312, false).unwrap();
    assert_eq!(events(dir.path())[0]["down_since"], 10);
}

#[test]
fn corrupt_state_and_clock_rollback_are_errors() {
    let dir = tempfile::tempdir().unwrap();
    observe(dir.path(), "build-a", "coordinator", 100, false).unwrap();
    assert!(observe(dir.path(), "build-a", "coordinator", 99, false).is_err());
    let state = fs::read_dir(dir.path().join("watchdog/host-link"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    fs::write(&state, "broken json").unwrap();
    assert!(observe(dir.path(), "build-a", "coordinator", 401, false).is_err());
    assert_eq!(fs::read_to_string(state).unwrap(), "broken json");
    assert!(events(dir.path()).is_empty());
}

#[test]
fn cli_rejects_option_peer_and_missing_tailscale_without_starting_outage() {
    let dir = tempfile::tempdir().unwrap();
    for peer in ["--help", "coordinator.example.test"] {
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
            .arg(dir.path())
            .args(["build-a", peer])
            .env("PATH", dir.path())
            .output()
            .unwrap();
        assert!(!result.status.success());
        assert!(!dir.path().join("watchdog").exists());
    }
}

#[cfg(unix)]
#[test]
fn state_and_alert_files_are_private() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    observe(dir.path(), "build-a", "coordinator", 10, false).unwrap();
    observe(dir.path(), "build-a", "coordinator", 311, false).unwrap();
    for folder in ["host-link", "outbox"] {
        let path = dir.path().join("watchdog").join(folder);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o700
        );
        for entry in fs::read_dir(path).unwrap() {
            assert_eq!(
                entry.unwrap().metadata().unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }
}

#[cfg(unix)]
#[test]
fn cli_failed_peer_queues_bridge_event_and_healthy_peer_resets_state() {
    use std::os::unix::fs::symlink;
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("state");
    let peer = dir.path().join("tailscale");
    symlink("/usr/bin/false", &peer).unwrap();
    let tick = || {
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
            .arg(&root)
            .args(["build-a", "coordinator"])
            .env("PATH", dir.path())
            .output()
            .unwrap();
        assert!(result.status.success(), "{:?}", result);
        String::from_utf8(result.stdout).unwrap()
    };
    assert!(tick().contains("reachable=false alert_queued=false"));
    let state = fs::read_dir(root.join("watchdog/host-link"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let mut value: Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
    value["down_since"] = Value::from(value["last_observed"].as_u64().unwrap() - 301);
    fs::write(&state, serde_json::to_vec(&value).unwrap()).unwrap();
    assert!(tick().contains("reachable=false alert_queued=true"));
    assert_eq!(events(&root).len(), 1);
    fs::remove_file(&peer).unwrap();
    symlink("/usr/bin/true", &peer).unwrap();
    assert!(tick().contains("reachable=true alert_queued=false"));
    let value: Value = serde_json::from_slice(&fs::read(state).unwrap()).unwrap();
    assert!(value["down_since"].is_null());
    assert_eq!(events(&root).len(), 1);
}
