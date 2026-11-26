//! End-to-end checks for the fixture-driven collector command.
use serde_json::{json, Value};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn readings() -> Value {
    json!({
        "now": 1_800_000_000,
        "usage": {"weighted": 1.0, "budget_per_day": 2.0},
        "ci": [],
        "keeper": {
            "items": [{"key":"demo:T","state":"open","epoch":0,"depends_on":[]}],
            "activity": []
        },
        "critical_target": "demo:T",
        "ready": {"items": []},
        "hosts": [{"host":"vm","checkout_head":"abc","origin_main_head":"abc",
                   "behind_commits":0,"age_hours":0.0,"disks":[]}],
        "units": [],
        "series": [
            {"host":"vm","ts":1_800_000_000,"runs":1,"ready":0,"reachable":true,"sessions":[],"cores":4,"sessions_lost":[],"top_rss":[]},
            {"host":"worker","ts":1_800_000_000,"runs":0,"ready":1,"reachable":true,"sessions":[],"cores":4,"sessions_lost":[],"top_rss":[]}
        ],
        "coding_hosts": ["vm"],
        "worker_hosts": ["worker"]
    })
}

fn command(readings: &Value, dir: &Path, input_name: &str) -> Command {
    let input = dir.join(input_name);
    fs::write(&input, readings.to_string()).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_plangraph"));
    command
        .args(["flow-snapshot", "--json", "--readings"])
        .arg(input)
        .arg("--out")
        .arg(dir.join("flow-snapshot.json"))
        .env("PLANGRAPH_ENV_FILE", dir.join("none.env"));
    command
}

fn success(output: &Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn fixture_cli_publishes_snapshot() {
    let dir = tempfile::tempdir().unwrap();
    let output = command(&readings(), dir.path(), "input.json")
        .output()
        .unwrap();
    let snapshot = success(&output);
    let published = fs::read(dir.path().join("flow-snapshot.json")).unwrap();
    assert_eq!(
        snapshot,
        serde_json::from_slice::<Value>(&published).unwrap()
    );
    assert_eq!(snapshot["budget"], 2.0);
    assert_eq!(snapshot["worker_idle"], true);
    assert_eq!(snapshot["coding_vm_runs"], 1);
    assert_eq!(snapshot["ready_families"], json!({}));
    assert!(snapshot.get("ordered_items").is_none());
    assert!(snapshot["unavailable_inputs"]["ordered_items"].is_string());
}

#[test]
fn failed_collection_preserves_valid_output() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("flow-snapshot.json");
    success(
        &command(&readings(), dir.path(), "valid.json")
            .output()
            .unwrap(),
    );
    let old = fs::read(&target).unwrap();
    let mut invalid = readings();
    invalid["usage"] = Value::Null;
    let failed = command(&invalid, dir.path(), "failed.json")
        .output()
        .unwrap();
    assert_eq!(failed.status.code(), Some(3));
    assert!(failed.stdout.is_empty());
    assert!(String::from_utf8_lossy(&failed.stderr).contains("usage"));
    assert_eq!(fs::read(&target).unwrap(), old);
}

#[test]
fn concurrent_publications_expose_only_complete_json() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("flow-snapshot.json");
    success(
        &command(&readings(), dir.path(), "first.json")
            .output()
            .unwrap(),
    );
    let mut children = Vec::new();
    for id in 0..8 {
        let mut reading = readings();
        reading["usage"]["weighted"] = json!(id);
        children.push(
            command(&reading, dir.path(), &format!("{id}.json"))
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .unwrap(),
        );
    }
    while children
        .iter_mut()
        .any(|child| child.try_wait().unwrap().is_none())
    {
        let current: Value = serde_json::from_slice(&fs::read(&target).unwrap()).unwrap();
        assert_eq!(current["budget"], 2.0);
        std::thread::yield_now();
    }
    for child in children {
        success(&child.wait_with_output().unwrap());
    }
    let current: Value = serde_json::from_slice(&fs::read(&target).unwrap()).unwrap();
    assert_eq!(current["budget"], 2.0);
}

#[test]
fn missing_readiness_keeps_independent_keeper_facts() {
    let dir = tempfile::tempdir().unwrap();
    let mut input = readings();
    input["keeper"]["items"][0]["owner"] = json!("build");
    input["keeper"]["activity"] = json!([{"type":"review_passed","ts":"2027-01-15T07:30:00Z"}]);
    input.as_object_mut().unwrap().remove("ready");
    input["source_errors"] = json!({"ready":"fixture unavailable"});
    let snapshot = success(
        &command(&input, dir.path(), "partial.json")
            .output()
            .unwrap(),
    );
    assert_eq!(snapshot["closes_recent"], 1);
    assert_eq!(snapshot["pending_by_role"]["build"], 1);
    assert_eq!(snapshot["lapsed_leases"], json!([]));
    assert!(snapshot.get("ready_families").is_none());
    assert_eq!(
        snapshot["unavailable_inputs"]["ready"],
        "ready: fixture unavailable"
    );
}
