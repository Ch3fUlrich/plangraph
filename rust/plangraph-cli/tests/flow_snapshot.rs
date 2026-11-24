//! `plangraph flow-snapshot` writes the snapshot atomically and says when it has gaps.
use serde_json::{json, Value};
use std::{fs, process::Command};

fn readings() -> Value {
    json!({"now":1_800_000_000.0,
      "usage":{"weighted":1.0,"budget_per_day":2.0},
      "ci":[], "items":[], "activity":[], "critical_path":[],
      "hosts":[], "units":[], "series":[],
      "coding_hosts":["vm"], "worker_hosts":["worker"]})
}

fn run(readings: &Value, dir: &std::path::Path) -> (i32, Value, String) {
    let input = dir.join("readings.json");
    let out = dir.join("state/flow-snapshot.json");
    fs::write(&input, readings.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["flow-snapshot", "--json", "--readings"])
        .arg(&input)
        .arg("--out")
        .arg(&out)
        .env("PLANGRAPH_ENV_FILE", dir.join("none.env"))
        .output()
        .unwrap();
    let file: Value = serde_json::from_slice(&fs::read(&out).unwrap()).unwrap();
    assert_eq!(
        file.to_string(),
        String::from_utf8_lossy(&output.stdout).trim()
    );
    (
        output.status.code().unwrap(),
        file,
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn a_complete_reading_writes_every_required_field() {
    let dir = tempfile::tempdir().unwrap();
    let (code, snapshot, stderr) = run(&readings(), dir.path());
    assert_eq!((code, stderr.as_str()), (0, ""));
    for name in [
        "pace",
        "budget",
        "ci_failed",
        "ci_total",
        "infra_failed",
        "review_age_hours",
        "review_rounds",
        "critical_idle_hours",
        "coding_vm_runs",
        "worker_idle",
        "host_drift",
        "closes_recent",
        "closes_previous",
    ] {
        assert!(snapshot.get(name).is_some(), "missing {name}");
    }
}

#[test]
fn a_failed_reading_is_reported_and_its_fields_are_left_out() {
    let dir = tempfile::tempdir().unwrap();
    let mut failed = readings();
    failed["usage"] = Value::Null;
    let (code, snapshot, stderr) = run(&failed, dir.path());
    assert_eq!(code, 3);
    assert!(stderr.contains("usage: usage reading failed"), "{stderr}");
    assert!(snapshot.get("pace").is_none() && snapshot.get("budget").is_none());
}
