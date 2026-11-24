#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;

use pg_worker_core::worker_host::{Phase, Store};
use serde_json::{json, Value};
use std::{fs, process::Command};

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
        pg_worker_core::executable_test_support::write_private_executable(
            &file,
            format!("#!/bin/sh\nprintf '%s\n' {program} >> \"$FIXTURE_ROOT/probe-log\"\nif [ ! -f \"$FIXTURE_ROOT/reachable\" ]; then exit 255; fi\n"),
        );
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

#[test]
fn delivery_continues_past_failed_events_and_quarantines_permanent_refusals() {
    let fixture = tempfile::tempdir().unwrap();
    let root = fixture.path();
    let config = root.join("plangraph");
    let state = root.join("state");
    let outbox = state.join("watchdog/outbox");
    fs::create_dir_all(&config).unwrap();
    fs::create_dir_all(&outbox).unwrap();
    fs::write(
        config.join("hosts.json"),
        json!({"version":1,"self":"observer","hosts":[{
            "id":"observer","aliases":[],"transport":"local","os":"linux",
            "state_dir":state,"session_mb":1,"roles":[],
            "caps":{"max_runs":1,"max_writers":0,"max_seats":0,"max_sessions":0,"mem_floor_mb":1},
            "warmup":{"warmup_s":1,"ramp_mb":{}},"repos":{}
        }]})
        .to_string(),
    )
    .unwrap();
    let names = ["retry", "refused", "success"].map(|name| {
        format!(
            "{:020}-worker-host-{name}.json",
            match name {
                "retry" => 1,
                "refused" => 2,
                _ => 3,
            }
        )
    });
    for name in &names {
        fs::write(outbox.join(name), "{}").unwrap();
    }
    let bridge = root.join("plangraph-bridge");
    fs::create_dir(&bridge).unwrap();
    let executable = bridge.join("plangraph");
    pg_worker_core::executable_test_support::write_private_executable(
        &executable,
        "#!/bin/sh\ncase \"$2\" in\n*-retry.json) exit 1;;\n*-refused.json) exit 3;;\nesac\n",
    );
    let result = Command::new(binary::spawnd())
        .args(["worker-liveness", "--deliver"])
        .env("PLANGRAPH_SPAWN_STATE", &state)
        .env("XDG_CONFIG_HOME", root)
        .env("PATH", &bridge)
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert!(outbox.join(&names[0]).exists());
    assert!(state.join("watchdog/rejected").join(&names[1]).exists());
    assert!(!outbox.join(&names[1]).exists());
    assert!(!outbox.join(&names[2]).exists());
}

#[test]
fn rejoin_settles_the_unknown_run_and_lifts_the_placement_hold() {
    use pg_worker_core::worker_host::Observation;
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
        r#"{"run_id":"run-example","host":"worker","status":"running","events":[]}"#,
    )
    .unwrap();
    let ssh = bin.join("ssh");
    fs::write(
        &ssh,
        r#"#!/bin/sh
case "$*" in
*worker-active-runs*) echo '{"host":"worker","runs":[]}';;
*--changed-since*) echo '{"rows":[{"run_id":"run-example","status":"completed","phase":"ended"}],"next_cursor":1,"more":false}';;
*work-disk*) echo '"/mnt/work"';;
*"probe --host-id"*) echo '{"mem_available_mb":4096}';;
esac
"#,
    )
    .unwrap();
    fs::set_permissions(&ssh, fs::Permissions::from_mode(0o700)).unwrap();

    let store = Store::new(&state, "worker").unwrap();
    let tick = |now, ssh| {
        store
            .observe_liveness(Observation {
                now,
                ssh,
                tailscale: None,
                remote_runs: None,
                placed_runs: if ssh {
                    Default::default()
                } else {
                    ["run-example".to_string()].into()
                },
            })
            .unwrap()
    };
    tick(10, false);
    assert_eq!(tick(20, false), Phase::Unreachable);
    assert_eq!(tick(900, true), Phase::Rejoining);
    let rejoin = || {
        Command::new(binary::spawnd())
            .args(["rejoin", "worker"])
            .env("PLANGRAPH_SPAWN_STATE", &state)
            .env("XDG_CONFIG_HOME", root)
            .env("PATH", &bin)
            .output()
            .unwrap()
    };
    let result = rejoin();
    assert!(result.status.success(), "{result:?}");
    let record: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(
        record["runs"],
        json!([{"run":"run-example","fate":"finished"}])
    );
    assert_eq!(store.phase().unwrap(), Phase::Ready);
    let settled: Value =
        serde_json::from_slice(&fs::read(row.join("record.json")).unwrap()).unwrap();
    assert_eq!(settled["status"], "completed");
    // A ready host has nothing to rejoin.
    assert!(!rejoin().status.success());
}
