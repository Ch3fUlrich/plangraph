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

#[cfg(unix)]
#[test]
fn ssh_watch_alerts_once_with_peer_and_ip_evidence_then_recovers() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::process::Command;

    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("state");
    let ssh = dir.path().join("ssh");
    symlink("/usr/bin/false", &ssh).unwrap();
    for (program, line) in [
        (
            "tailscale",
            r#"{"Peer":{"node":{"HostName":"peer-a","Online":false,"Active":true,"Relay":"fra"}}}"#,
        ),
        (
            "journalctl",
            "portmap: monitor: gateway and self IP changed: fixture",
        ),
    ] {
        let path = dir.path().join(program);
        fs::write(&path, format!("#!/bin/sh\nprintf '%s\\n' '{line}'\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let tick = || {
        let result = Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
            .args(["ssh-tick"])
            .arg(&root)
            .args(["observer", "worker", "user@worker", "peer-a"])
            .env("PATH", dir.path())
            .output()
            .unwrap();
        assert!(result.status.success(), "{result:?}");
        String::from_utf8(result.stdout).unwrap()
    };

    tick();
    tick();
    assert!(
        events(&root).is_empty(),
        "remote nonzero exit proves SSH connected"
    );
    fs::remove_file(&ssh).unwrap();
    fs::write(&ssh, "#!/bin/sh\nexit 255\n").unwrap();
    fs::set_permissions(&ssh, fs::Permissions::from_mode(0o700)).unwrap();

    tick();
    assert!(events(&root).is_empty());
    let alert_line = tick();
    assert!(alert_line.contains("worker unreachable since "));
    assert!(!tick().contains("unreachable since "));
    let status = || {
        Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
            .arg("ssh-status")
            .arg(&root)
            .arg("user@worker")
            .output()
            .unwrap()
    };
    let unavailable = status();
    assert!(!unavailable.status.success());
    assert!(String::from_utf8_lossy(&unavailable.stderr).contains("user@worker unreachable since "));
    let alerts = events(&root);
    assert_eq!(alerts.len(), 1);
    assert_eq!(alerts[0]["rule"], "host-ssh-down");
    assert_eq!(alerts[0]["peer_state"]["Online"], false);
    assert_eq!(alerts[0]["peer_state"]["Relay"], "fra");
    assert!(alerts[0]["last_ip_change"]
        .as_str()
        .unwrap()
        .contains("gateway and self IP changed"));
    fs::remove_file(&ssh).unwrap();
    symlink("/usr/bin/true", &ssh).unwrap();
    assert!(tick().contains("worker recovered"));
    assert!(!tick().contains("worker recovered"));
    assert!(status().status.success());
    assert_eq!(
        events(&root)
            .iter()
            .filter(|event| event["rule"] == "host-ssh-recovered")
            .count(),
        1
    );
}

#[cfg(unix)]
#[test]
fn ssh_wait_retries_255_then_succeeds_and_names_a_persistent_outage() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    let dir = tempfile::tempdir().unwrap();
    let ssh = dir.path().join("ssh");
    fs::write(
        &ssh,
        "#!/bin/sh\nread n < attempts\nn=$((n + 1))\nprintf '%s\\n' \"$n\" > attempts\nif test \"$n\" = \"$RECOVER_ON\"; then printf 'ready\\n'; exit 0; fi\nexit 255\n",
    ).unwrap();
    fs::set_permissions(ssh, fs::Permissions::from_mode(0o700)).unwrap();
    let run = |recover_on: &str| {
        fs::write(dir.path().join("attempts"), "0\n").unwrap();
        Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
            .arg("ssh-wait")
            .arg(dir.path())
            .args(["worker", "--", "true"])
            .env("PATH", dir.path())
            .env("RECOVER_ON", recover_on)
            .current_dir(dir.path())
            .output()
            .unwrap()
    };
    let recovered = run("2");
    assert!(recovered.status.success(), "{recovered:?}");
    assert_eq!(recovered.stdout, b"ready\n");
    let failed = run("0");
    assert_eq!(failed.status.code(), Some(255));
    assert!(String::from_utf8_lossy(&failed.stderr).contains("worker unreachable since "));
    assert_eq!(
        fs::read_to_string(dir.path().join("attempts")).unwrap(),
        "3\n"
    );
}

#[test]
fn bridge_reads_a_queued_outage_before_its_recovery() {
    use pg_worker_core::host_link::observe_ssh;
    let dir = tempfile::tempdir().unwrap();
    for (now, reachable) in [(101, false), (161, false), (221, true)] {
        observe_ssh(dir.path(), "user@worker", now, reachable, || {
            serde_json::json!({})
        })
        .unwrap();
    }
    let mut entries: Vec<_> = fs::read_dir(dir.path().join("watchdog/outbox"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    entries.sort();
    let rules: Vec<_> = entries
        .iter()
        .map(|path| {
            let event: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
            event["rule"].as_str().unwrap().to_owned()
        })
        .collect();
    assert_eq!(rules, ["host-ssh-down", "host-ssh-recovered"]);
}

#[test]
fn incomplete_ssh_subcommand_prints_usage() {
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_pg-host-link"))
        .args(["ssh-wait", "state", "worker"])
        .env("PATH", "")
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("usage:"));
}

#[test]
fn peer_and_ssh_probes_keep_independent_observation_history() {
    use pg_worker_core::host_link::observe_ssh;

    let state = tempfile::tempdir().unwrap();
    observe(state.path(), "ssh", "worker", 100, false).unwrap();
    assert_eq!(
        observe_ssh(state.path(), "worker", 110, false, || serde_json::json!({})).unwrap(),
        None
    );
    assert_eq!(
        observe_ssh(state.path(), "worker", 170, false, || serde_json::json!({})).unwrap(),
        Some(110)
    );
}
