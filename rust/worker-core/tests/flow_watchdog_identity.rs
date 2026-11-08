#![cfg(unix)]
use std::{fs, path::Path, process::Command};

fn snapshot() -> serde_json::Value {
    serde_json::json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":true,
        "host_drift":false,"closes_recent":0,"closes_previous":0})
}
fn run(dir: &Path, s: &serde_json::Value, hooks: bool) -> std::process::Output {
    fs::write(dir.join("input.json"), s.to_string()).unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"));
    cmd.arg(dir.join("input.json")).arg(dir);
    for key in ["PLANGRAPH_WATCHDOG_DELIVERY", "PLANGRAPH_FLOW_PUSH"] {
        if hooks { cmd.env(key, "[\"/bin/cat\"]"); } else { cmd.env_remove(key); }
    }
    cmd.output().unwrap()
}
#[test]
fn standing_conditions_have_stable_single_line_identities() {
    let d = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    s["lapsed_leases"] = serde_json::json!([{"item":"item\n1","owner":"owner\n1",
        "age_minutes":31,"open_lane":true}]);
    s["hosts"] = serde_json::json!([{"host":"fixture\n1","tmp_used_percent":86,
        "root_used_percent":0,"checkout_head":"a\n1","origin_main_head":"b\n1",
        "behind_commits":11,"age_hours":25}]);
    assert!(run(d.path(), &s, false).status.success());
    let before = fs::read_to_string(d.path().join("active")).unwrap();
    assert_eq!(before.lines().count(), 3);
    s["lapsed_leases"][0]["age_minutes"] = 100.into();
    s["hosts"][0]["tmp_used_percent"] = 99.into();
    s["hosts"][0]["age_hours"] = 100.into();
    s["hosts"][0]["behind_commits"] = 20.into();
    s["hosts"][0]["checkout_head"] = "c\n1".into();
    assert!(run(d.path(), &s, false).status.success());
    assert_eq!(fs::read_to_string(d.path().join("active")).unwrap(), before);
    let queue: Vec<serde_json::Value> = serde_json::from_slice(
        &fs::read(d.path().join("queue.json")).unwrap()).unwrap();
    assert_eq!(queue.len(), 3);
    assert!(run(d.path(), &snapshot(), true).status.success());
    assert!(run(d.path(), &s, false).status.success());
    let queue: Vec<serde_json::Value> = serde_json::from_slice(
        &fs::read(d.path().join("queue.json")).unwrap()).unwrap();
    assert_eq!(queue.len(), 3);
}
#[test]
fn invalid_snapshot_is_durable_and_retried_after_recovery() {
    let d = tempfile::tempdir().unwrap();
    assert!(!run(d.path(), &serde_json::json!({}), false).status.success());
    let queue: Vec<serde_json::Value> = serde_json::from_slice(
        &fs::read(d.path().join("queue.json")).unwrap()).unwrap();
    assert_eq!(queue.len(), 1);
    assert_eq!(queue[0]["message"], "FLOW-WATCHDOG: invalid snapshot");
    assert!(!run(d.path(), &serde_json::json!({}), false).status.success());
    let failed = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(d.path().join("input.json")).arg(d.path())
        .env("PLANGRAPH_WATCHDOG_DELIVERY", "[\"/bin/false\"]")
        .env("PLANGRAPH_FLOW_PUSH", "[\"/bin/false\"]").output().unwrap();
    assert!(!failed.status.success());
    assert!(run(d.path(), &snapshot(), true).status.success());
    assert_eq!(fs::read_to_string(d.path().join("queue.json")).unwrap(), "[]");
}
