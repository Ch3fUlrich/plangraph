#![cfg(unix)]

use std::{fs, process::Command};
fn run(snapshot: &serde_json::Value, dir: &std::path::Path) -> std::process::Output {
    let input = dir.join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .args([input.to_str().unwrap(), dir.to_str().unwrap()])
        .output()
        .unwrap()
}

#[test]
fn thresholds_and_deduplicated_delivery() {
    let dir = tempfile::tempdir().unwrap();
    let safe = serde_json::json!({"pace":66.0,"budget":66.0,
        "ci_failed":1,"ci_total":10,"infra_failed":0,
        "review_age_hours":24.0,"review_rounds":3,"critical_idle_hours":12.0,
        "coding_vm_runs":0,"worker_idle":true,"host_drift":false,
        "closes_recent":4,"closes_previous":4});
    assert!(run(&safe, dir.path()).status.success());
    assert!(!dir.path().join("pending.json").exists());
    let bad = serde_json::json!({"pace":242.0,"budget":66.0,
        "ci_failed":2,"ci_total":10,"infra_failed":0,
        "review_age_hours":24.1,"review_rounds":4,"critical_idle_hours":12.1,
        "coding_vm_runs":1,"worker_idle":true,"host_drift":true,
        "closes_recent":3,"closes_previous":4});
    let first = run(&bad, dir.path());
    assert!(first.status.success(), "{:?}", first);
    let pending = fs::read_to_string(dir.path().join("pending.json")).unwrap();
    let event: serde_json::Value = serde_json::from_str(&pending).unwrap();
    let line = event["message"].as_str().unwrap();
    assert_eq!(line.lines().count(), 1);
    for name in [
        "claude-pace",
        "ci-fail-rate",
        "review-age",
        "review-rounds",
        "critical-path-idle",
        "placement",
        "host-drift",
        "closes-trend",
    ] {
        assert!(line.contains(name), "{line}");
    }
    assert!(run(&bad, dir.path()).status.success());
    assert_eq!(
        pending,
        fs::read_to_string(dir.path().join("pending.json")).unwrap()
    );
    let deliver = || {
        Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
            .arg(dir.path().join("input.json"))
            .arg(dir.path())
            .env("PLANGRAPH_WATCHDOG_DELIVERY", "[\"/bin/cat\"]")
            .env("PLANGRAPH_FLOW_PUSH", "[\"/bin/cat\"]")
            .output()
            .unwrap()
    };
    assert!(deliver().status.success());
    assert!(!dir.path().join("pending.json").exists());
    assert!(deliver().status.success());
    assert!(!dir.path().join("pending.json").exists());
    assert!(run(&safe, dir.path()).status.success());
    assert_eq!(fs::read_to_string(dir.path().join("active")).unwrap(), "");
    assert!(run(&bad, dir.path()).status.success());
    assert!(dir.path().join("pending.json").exists());
}
