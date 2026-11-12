#![cfg(unix)]

use serde_json::{json, Value};
use std::{fs, process::Command};
const NOW: u64 = 1767225600;

#[test]
fn opt_in_reader_queues_one_line_and_under_budget_queues_none() {
    let dir = tempfile::tempdir().unwrap();
    let fleet = dir.path().join("fleet");
    fs::create_dir_all(fleet.join("status")).unwrap();
    let status = |wake: u64| {
        json!({"turn_budget":{"session":"build-L1-0429",
        "role":"build-L1","session_id":"test-session","wake_calls":wake,
        "hour_calls":2,"calls":[NOW,NOW]}})
    };
    let path = fleet.join("status/test-session.json");
    fs::write(&path, status(7).to_string()).unwrap();
    let input = dir.path().join("snapshot.json");
    fs::write(
        &input,
        json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,
        "coding_vm_runs":0,"worker_idle":false,"host_drift":false,
        "closes_recent":0,"closes_previous":0})
        .to_string(),
    )
    .unwrap();
    let run = |state: &str, opt_in: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"));
        command.env_clear().arg(&input).arg(dir.path().join(state));
        if opt_in {
            command.env("PLANGRAPH_TURN_BUDGET_STATE_DIR", &fleet);
            command.env("PLANGRAPH_TURN_BUDGET_NOW", NOW.to_string());
        }
        assert!(command.output().unwrap().status.success());
    };
    run("disabled", false);
    assert!(!dir.path().join("disabled/queue.json").exists());
    run("over", true);
    let queue_path = dir.path().join("over/queue.json");
    let queue: Value = serde_json::from_slice(&fs::read(&queue_path).unwrap()).unwrap();
    assert_eq!(queue.as_array().unwrap().len(), 1);
    assert_eq!(
        queue[0]["message"],
        "TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=2/60"
    );
    run("over", true);
    assert_eq!(
        queue,
        serde_json::from_slice::<Value>(&fs::read(&queue_path).unwrap()).unwrap()
    );
    for wake in [8, 9, 10] {
        fs::write(&path, status(wake).to_string()).unwrap();
        run("over", true);
        let queue: Value = serde_json::from_slice(&fs::read(&queue_path).unwrap()).unwrap();
        assert_eq!(queue.as_array().unwrap().len(), 1);
        let expected = format!("TURN-BUDGET build-L1 build-L1-0429 wake={wake}/6 hour=2/60");
        assert_eq!(queue[0]["message"], expected);
        let pending: Value =
            serde_json::from_slice(&fs::read(dir.path().join("over/pending.json")).unwrap())
                .unwrap();
        assert_eq!(pending["message"], expected);
        assert_eq!(
            fs::read_to_string(dir.path().join("over/active")).unwrap(),
            expected
        );
    }
    fs::write(&path, status(6).to_string()).unwrap();
    run("under", true);
    assert!(!dir.path().join("under/queue.json").exists());

    // Successful delivery must not reopen the alert when only counts change.
    fs::write(&path, status(11).to_string()).unwrap();
    let deliveries = dir.path().join("deliveries.jsonl");
    let bridge = json!([
        "/bin/sh",
        "-c",
        "cat >> \"$1\"; printf '\\n' >> \"$1\"",
        "fixture",
        deliveries
    ]);
    assert!(Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_clear()
        .arg(&input)
        .arg(dir.path().join("over"))
        .env("PLANGRAPH_TURN_BUDGET_STATE_DIR", &fleet)
        .env("PLANGRAPH_TURN_BUDGET_NOW", NOW.to_string())
        .env("PLANGRAPH_WATCHDOG_DELIVERY", "[\"/bin/cat\"]")
        .env("PLANGRAPH_FLOW_PUSH", bridge.to_string())
        .output()
        .unwrap()
        .status
        .success());
    assert_eq!(fs::read_to_string(&queue_path).unwrap(), "[]");
    assert_eq!(fs::read_to_string(deliveries).unwrap().lines().count(), 1);
    fs::write(&path, status(12).to_string()).unwrap();
    run("over", true);
    assert_eq!(fs::read_to_string(&queue_path).unwrap(), "[]");
    fs::write(&path, status(6).to_string()).unwrap();
    run("over", true);
    assert_eq!(
        fs::read_to_string(dir.path().join("over/active")).unwrap(),
        ""
    );
    fs::write(&path, status(7).to_string()).unwrap();
    run("over", true);
    let queue: Value = serde_json::from_slice(&fs::read(&queue_path).unwrap()).unwrap();
    assert_eq!(queue.as_array().unwrap().len(), 1);
}

#[test]
fn corrupt_pending_refresh_does_not_stop_other_alert_delivery() {
    for corrupt in [
        b"not json".to_vec(),
        b"\xff".to_vec(),
        json!({"padding":"x".repeat(1024 * 1024)})
            .to_string()
            .into_bytes(),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let fleet = dir.path().join("fleet");
        fs::create_dir_all(fleet.join("status")).unwrap();
        let state = dir.path().join("watchdog");
        fs::create_dir(&state).unwrap();
        let old = "TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=0/60";
        fs::write(
            state.join("queue.json"),
            json!([
                {"kind":"alert", "active":old, "message":old},
                {"kind":"alert", "active":"placement", "message":"FLOW-WATCHDOG: placement"}
            ])
            .to_string(),
        )
        .unwrap();
        fs::write(state.join("pending.json"), &corrupt).unwrap();
        fs::write(state.join("active"), format!("placement\n{old}")).unwrap();
        fs::write(
            fleet.join("status/test-session.json"),
            json!({"turn_budget":{
                "session":"build-L1-0429", "role":"build-L1", "wake_calls":8
            }})
            .to_string(),
        )
        .unwrap();
        let snapshot = dir.path().join("snapshot.json");
        fs::write(
            &snapshot,
            json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
            "infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,
            "coding_vm_runs":1,"worker_idle":true,"host_drift":false,
            "closes_recent":0,"closes_previous":0})
            .to_string(),
        )
        .unwrap();
        let delivered = dir.path().join("delivered");
        let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
            .env_clear()
            .arg(&snapshot)
            .arg(&state)
            .env("PLANGRAPH_TURN_BUDGET_STATE_DIR", &fleet)
            .env("PLANGRAPH_TURN_BUDGET_NOW", NOW.to_string())
            .env(
                "PLANGRAPH_WATCHDOG_DELIVERY",
                json!([
                    "/bin/sh",
                    "-c",
                    "cat >> \"$1\"; printf '\\n' >> \"$1\"",
                    "fixture",
                    delivered
                ])
                .to_string(),
            )
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let lines = fs::read_to_string(&delivered).unwrap();
        assert_eq!(lines.lines().count(), 2);
        assert!(lines.contains("FLOW-WATCHDOG: placement"));
        let queue: Value =
            serde_json::from_slice(&fs::read(state.join("queue.json")).unwrap()).unwrap();
        assert!(queue
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["routing_done"] == true));
        assert_eq!(fs::read(state.join("pending.json")).unwrap(), corrupt);
    }
}

#[test]
fn duplicate_budget_events_compact_by_identity_preserving_oldest_acknowledgements() {
    let dir = tempfile::tempdir().unwrap();
    let fleet = dir.path().join("fleet");
    fs::create_dir_all(fleet.join("status")).unwrap();
    let state = dir.path().join("watchdog");
    fs::create_dir(&state).unwrap();
    let line =
        |name: &str, wake: u64| format!("TURN-BUDGET build-L1 {name} wake={wake}/6 hour=0/60");
    let first = line("build-L1-0429", 7);
    let second = line("build-L1-0430", 7);
    let queue = json!([
        {"active":first, "message":first, "routing_done":true, "oldest":"first"},
        {"active":first, "message":first, "routing_done":false},
        {"active":"placement", "message":"FLOW-WATCHDOG: placement"},
        {"active":second, "message":second, "routing_done":false, "oldest":"second"},
        {"active":second, "message":second, "routing_done":true}
    ]);
    fs::write(state.join("queue.json"), queue.to_string()).unwrap();
    fs::write(state.join("pending.json"), queue[0].to_string()).unwrap();
    fs::write(state.join("active"), format!("{first}\n{second}")).unwrap();
    for name in ["build-L1-0429", "build-L1-0430"] {
        fs::write(
            fleet.join("status").join(format!("{name}.json")),
            json!({"turn_budget":{
                "session":name, "role":"build-L1", "wake_calls":8
            }})
            .to_string(),
        )
        .unwrap();
    }
    let snapshot = dir.path().join("snapshot.json");
    fs::write(
        &snapshot,
        json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,
        "coding_vm_runs":0,"worker_idle":false,"host_drift":false,
        "closes_recent":0,"closes_previous":0})
        .to_string(),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_clear()
        .arg(&snapshot)
        .arg(&state)
        .env("PLANGRAPH_TURN_BUDGET_STATE_DIR", &fleet)
        .env("PLANGRAPH_TURN_BUDGET_NOW", NOW.to_string())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let queue: Value =
        serde_json::from_slice(&fs::read(state.join("queue.json")).unwrap()).unwrap();
    assert_eq!(queue.as_array().unwrap().len(), 3);
    assert_eq!(queue[0]["oldest"], "first");
    assert_eq!(queue[0]["routing_done"], true);
    assert_eq!(queue[0]["message"], line("build-L1-0429", 8));
    assert_eq!(queue[1]["active"], "placement");
    assert_eq!(queue[2]["oldest"], "second");
    assert_eq!(queue[2]["routing_done"], false);
    assert_eq!(queue[2]["message"], line("build-L1-0430", 8));
}
