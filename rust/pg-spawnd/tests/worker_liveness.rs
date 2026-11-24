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
    let queued = state.join("runs/run-queued");
    let live = state.join("runs/run-live");
    let dead = state.join("runs/run-dead");
    for dir in [&config, &row, &queued, &live, &dead, &bin] {
        fs::create_dir_all(dir).unwrap();
    }
    let host = |id: &str, transport: &str| {
        json!({
            "id":id,"aliases":[],"transport":transport,"os":"linux",
            "state_dir":state,"session_mb":1,"roles":["implement"],
            "caps":{"max_runs":2,"max_writers":1,"max_seats":1,"max_sessions":1,"mem_floor_mb":1},
            "warmup":{"warmup_s":1,"ramp_mb":{"implement":1}},
            "repos":{"plangraph":{
                "root":"/srv/plangraph","worktrees_root":"/srv/worktrees",
                "remote":"origin","import_roots":[],"cache_env":{}}},
            "env":{"PG_INSTALL_BINARY":"/opt/pg-spawnd","PG_INSTALL_STAMP":"/opt/pg-spawnd.stamp",
                   "PLANGRAPH_WORK_MOUNTS":"/mnt/work",
                   "PG_REJOIN_LEG_PROBES":"{\"codex\":[\"leg-probe\",\"codex\"]}",
                   "PG_INSTALL_HOOKS":"{\"turn\":\"/opt/hook\"}"}
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
    fs::write(
        queued.join("record.json"),
        r#"{"run_id":"run-queued","host":"worker","status":"queued","events":[]}"#,
    )
    .unwrap();
    fs::write(
        live.join("record.json"),
        r#"{"run_id":"run-live","host":"worker","status":"unknown","events":[]}"#,
    )
    .unwrap();
    fs::write(
        dead.join("record.json"),
        r#"{"run_id":"run-dead","host":"worker","status":"running","item_key":"plangraph:ITEM","events":[]}"#,
    )
    .unwrap();
    pg_worker_core::executable_test_support::write_private_executable(
        bin.join("keeper-cli"),
        r#"#!/bin/sh
case "$1" in
release) echo "$2" > "$FIXTURE_ROOT/released";;
show) if [ -f "$FIXTURE_ROOT/released" ]; then echo '{"state":"open"}'; else echo '{"state":"claimed"}'; fi;;
esac
"#,
    );
    pg_worker_core::executable_test_support::write_private_executable(
        bin.join("ssh"),
        r#"#!/bin/sh
# Any command the gate was not expected to run fails the rejoin.
case "$*" in
*check-install*) echo '{"event":"pg_spawnd_install","status":"stale","reason":"source_sha_mismatch"}';;
*worker-active-runs*) echo '{"host":"worker","runs":[],"live":["run-live"],"dead":["run-dead"],"terminal":{"run-example":{"status":"completed","ended":123.5,"exit_code":0},"run-queued":{"status":"limited","ended":null,"exit_code":null}}}';;
*"sha256sum /opt/hook"*) echo "bbbb  /opt/hook";;
*"--reap"*) ;;
*"rev-parse origin/main"*) echo bbbb;;
*"leg-probe codex"*) ;;
*"rev-parse HEAD"*) if [ -f "$FIXTURE_ROOT/synced" ]; then echo bbbb; else echo aaaa; fi;;
*"start plangraph-host-sync.service"*) : > "$FIXTURE_ROOT/synced";;
*"start pg-worktree-sweep.service"*) ;;
*"PLANGRAPH_WORKTREE_MOUNTS=/mnt/work pg-spawnd host work-disk"*) echo '"/mnt/work"';;
*"probe --host-id"*) echo '{"mem_available_mb":4096}';;
*) echo "unexpected: $*" >&2; exit 1;;
esac
"#,
    );

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
                    ["run-example", "run-queued", "run-live", "run-dead"]
                        .map(String::from)
                        .into()
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
            .env("FIXTURE_ROOT", root)
            .env("PG_KEEPER_CLI", bin.join("keeper-cli"))
            .env("PG_MAIN_HOOK_SHA256", r#"{"turn":"aaaa"}"#)
            .env("PG_MAIN_SHA", "a".repeat(40))
            .env("PG_MAIN_SHA256", "b".repeat(64))
            .output()
            .unwrap()
    };
    let result = rejoin();
    assert!(result.status.success(), "{result:?}");
    let record: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(
        record["runs"],
        json!([
            {"run":"run-dead","fate":"dead"},
            {"run":"run-example","fate":"finished"},
            {"run":"run-live","fate":"live"},
            {"run":"run-queued","fate":"finished"}
        ])
    );
    // The stale checkout was fast-forwarded by the host sync.
    assert_eq!(
        record["steps"][0]["detail"]["fast_forwarded"],
        json!(["plangraph"])
    );
    // A stale installed binary is flagged with check-install's reason, not blocked.
    assert!(record["flags"][0]
        .as_str()
        .unwrap()
        .starts_with("binary: source_sha_mismatch"));
    assert!(record["flags"][0]
        .as_str()
        .unwrap()
        .contains("stale hooks: turn"));
    // The dead run's Keeper claim was released and read back.
    assert_eq!(
        fs::read_to_string(root.join("released")).unwrap().trim(),
        "plangraph:ITEM"
    );
    assert_eq!(store.phase().unwrap(), Phase::Ready);
    let settled: Value =
        serde_json::from_slice(&fs::read(row.join("record.json")).unwrap()).unwrap();
    assert_eq!(settled["status"], "completed");
    // The host's own result is kept: when it ended and its exit code.
    assert_eq!(settled["ended"], 123.5);
    assert_eq!(settled["exit_code"], 0);
    // A run that vanished is failed so its claim and lane are released.
    let gone: Value = serde_json::from_slice(&fs::read(dead.join("record.json")).unwrap()).unwrap();
    assert_eq!(gone["status"], "failed");
    // A live run goes back to running with a re-attach event.
    let live: Value = serde_json::from_slice(&fs::read(live.join("record.json")).unwrap()).unwrap();
    assert_eq!(live["status"], "running");
    assert_eq!(live["events"][0]["event"], "REJOIN_REATTACHED");
    // A queued run that ended limited cannot walk queued -> limited; it is settled as failed.
    let queued: Value =
        serde_json::from_slice(&fs::read(queued.join("record.json")).unwrap()).unwrap();
    assert_eq!(queued["status"], "failed");
    // A ready host has nothing to rejoin.
    assert!(!rejoin().status.success());
}
