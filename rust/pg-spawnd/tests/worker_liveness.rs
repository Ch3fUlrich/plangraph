#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;

use pg_worker_core::worker_host::{Phase, Store};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, process::Command};

#[test]
fn two_misses_emit_one_alert_and_recovery_keeps_the_affected_runs() {
    let fixture = tempfile::tempdir().unwrap();
    let root = fixture.path();
    let config = root.join("plangraph");
    let state = root.join("state");
    let bin = root.join("bin");
    let row = state.join("runs/run-example");
    for dir in [&config, &row, &bin] {
        fs::create_dir_all(dir).unwrap();
    }
    let host = |id: &str, transport: &str| {
        json!({
            "id":id,"aliases":[],"transport":transport,"os":"linux",
            "state_dir":state,"session_mb":1,"roles":["implement"],
            "caps":{"max_runs":2,"max_writers":1,"max_seats":1,"max_sessions":1,"mem_floor_mb":1},
            "warmup":{"warmup_s":1,"ramp_mb":{"implement":1}},
            "repos":{}
        })
    };
    fs::write(
        config.join("hosts.json"),
        json!({"version":1,"self":"observer","hosts":[host("observer","local"),host("worker","ssh")]}).to_string(),
    )
    .unwrap();
    fs::write(
        row.join("record.json"),
        r#"{"run_id":"run-example","host":"worker","status":"running"}"#,
    )
    .unwrap();
    for program in ["ssh", "tailscale"] {
        let file = bin.join(program);
        fs::write(
            &file,
            format!("#!/bin/sh\nprintf '%s\n' {program} >> \"$FIXTURE_ROOT/probe-log\"\nif [ ! -f \"$FIXTURE_ROOT/reachable\" ]; then exit 255; fi\n"),
        )
        .unwrap();
        fs::set_permissions(file, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let tick = || {
        let result = Command::new(binary::spawnd())
            .arg("worker-liveness")
            .env("PLANGRAPH_SPAWN_STATE", &state)
            .env("XDG_CONFIG_HOME", root)
            .env("FIXTURE_ROOT", root)
            .env("PATH", &bin)
            .output()
            .unwrap();
        assert!(result.status.success(), "{result:?}");
    };
    let store = Store::new(&state, "worker").unwrap();
    let events = || {
        fs::read_dir(state.join("watchdog/outbox"))
            .into_iter()
            .flatten()
            .map(|entry| -> Value {
                serde_json::from_slice(&fs::read(entry.unwrap().path()).unwrap()).unwrap()
            })
            .collect::<Vec<_>>()
    };
    tick();
    assert!(events().is_empty());
    tick();
    assert_eq!(store.phase().unwrap(), Phase::Unreachable);
    tick();
    assert_eq!(events().len(), 1);
    assert_eq!(events()[0]["unknown_run_ids"], json!(["run-example"]));

    fs::remove_file(row.join("record.json")).unwrap();
    fs::write(root.join("reachable"), "").unwrap();
    tick();
    assert_eq!(store.phase().unwrap(), Phase::Ready);
    let emitted = events();
    assert_eq!(emitted.len(), 2);
    let clear = emitted
        .iter()
        .find(|event| event["rule"] == "worker-host-recovered")
        .unwrap();
    assert_eq!(clear["unknown_run_ids"], json!(["run-example"]));
    let calls = fs::read_to_string(root.join("probe-log")).unwrap();
    assert_eq!(calls.lines().filter(|line| *line == "ssh").count(), 5);
    assert_eq!(calls.lines().filter(|line| *line == "tailscale").count(), 4);
}
