#![cfg(unix)]
//! The collector's snapshot is accepted by the flow-watchdog checker, and every
//! required field is load-bearing for that acceptance.
use pg_worker_core::flow_collect::{assemble, Readings};
use serde_json::{json, Value};
use std::{fs, process::Command};

const NOW: f64 = 1_800_000_000.0;
const REQUIRED: [&str; 13] = [
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
];

fn iso(ago: f64) -> String {
    chrono::DateTime::from_timestamp((NOW - ago) as i64, 0)
        .unwrap()
        .to_rfc3339()
}

fn readings() -> Value {
    json!({
      "now": NOW,
      "usage": {"weighted": 40.0, "budget_per_day": 100.0},
      "ci": [
        {"conclusion":"success","created":iso(3600.0)},
        {"conclusion":"failure","created":iso(7200.0)},
        {"conclusion":"startup_failure","created":iso(9000.0)},
        {"conclusion":"success","created":iso(40.0 * 86400.0)},
        {"conclusion":"cancelled","created":iso(100.0)}],
      "items": [
        {"key":"demo:A","state":"open","owner":"build","updated_at":iso(600.0),"depends_on":[]},
        {"key":"demo:B","state":"open","owner":"build","updated_at":iso(60.0),"depends_on":"demo:A"},
        {"key":"other:C","state":"open","owner":"plan","updated_at":iso(7200.0),"depends_on":"[\"demo:D\"]"},
        {"key":"demo:D","state":"done","updated_at":iso(9000.0)},
        {"key":"demo:E","state":"claimed","owner":"build","claimed_by":"writer-1",
         "lease_until":iso(1800.0),"updated_at":iso(4000.0)},
        {"key":"demo:F","state":"in_review","owner":"build","merged":true,"updated_at":iso(7200.0)}],
      "activity": [
        {"ts":iso(3600.0),"key":"demo:D","type":"review_passed"},
        {"ts":iso(30.0 * 3600.0),"key":"demo:G","type":"closed_on_decision"},
        {"ts":iso(50.0 * 3600.0),"key":"demo:H","type":"answered"},
        {"ts":iso(2000.0),"key":"demo:F","type":"changes_requested"},
        {"ts":iso(1000.0),"key":"demo:F","type":"changes_requested"}],
      "critical_path": ["demo:A","demo:E"],
      "hosts": [{"host":"box","tmp_used_percent":10.0,"root_used_percent":20.0,
                 "checkout_head":"aaa","origin_main_head":"aaa","behind_commits":0,"age_hours":0.0}],
      "units": [{"host":"box","name":"w.service","deployed_sha":"s1","expected_sha":"s1"}],
      "series": [{"host":"vm","ts":NOW,"runs":2,"ready":0,"reachable":true,"sessions":[]},
                 {"host":"worker","ts":NOW,"runs":0,"ready":3,"reachable":true,"sessions":["plan-0001"]}],
      "coding_hosts": ["vm"], "worker_hosts": ["worker"]
    })
}

fn collect(value: Value) -> (Value, Vec<String>) {
    let readings: Readings = serde_json::from_value(value).unwrap();
    let assembled = assemble(&readings);
    (assembled.snapshot, assembled.gaps)
}

fn check(snapshot: &Value) -> (bool, String) {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("flow-snapshot.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_remove("PLANGRAPH_WATCHDOG_PROC_ROOT")
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_TURN_BUDGET_STATE_DIR")
        .args([
            input.to_str().unwrap(),
            dir.path().join("state").to_str().unwrap(),
        ])
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    (!stderr.contains("invalid snapshot"), stderr)
}

#[test]
fn the_checker_accepts_a_collected_snapshot_and_each_required_field_matters() {
    let (snapshot, gaps) = collect(readings());
    assert!(gaps.is_empty(), "{gaps:?}");
    for name in REQUIRED {
        assert!(snapshot.get(name).is_some(), "collector left out {name}");
    }
    assert_eq!(check(&snapshot), (true, String::new()));
    for name in REQUIRED {
        let mut cut = snapshot.clone();
        cut.as_object_mut().unwrap().remove(name);
        assert!(!check(&cut).0, "removing {name} was accepted");
    }
}

#[test]
fn derived_values_follow_the_documented_windows() {
    let (s, _) = collect(readings());
    assert_eq!(
        (
            s["ci_total"].clone(),
            s["ci_failed"].clone(),
            s["infra_failed"].clone()
        ),
        (json!(3), json!(2), json!(1))
    );
    assert_eq!(
        (s["closes_recent"].clone(), s["closes_previous"].clone()),
        (json!(1), json!(1))
    );
    assert_eq!(s["review_rounds"], 3);
    assert_eq!(s["review_age_hours"], 2.0);
    assert_eq!(
        (s["coding_vm_runs"].clone(), s["worker_idle"].clone()),
        (json!(2), json!(true))
    );
    assert_eq!(s["host_drift"], false);
    assert!((s["critical_idle_hours"].as_f64().unwrap() - 600.0 / 3600.0).abs() < 1e-9);
    assert_eq!(
        s["ready_families"],
        json!({"build":["demo"],"plan":["other"]})
    );
    assert_eq!(
        s["lapsed_leases"][0],
        json!({"item":"demo:E","owner":"writer-1","age_minutes":30,"open_lane":true})
    );
    assert_eq!(s["merged_reviews"][0]["age_minutes"], 120);
    assert_eq!(s["idle_capacity"][0]["owner"], "worker");
    assert_eq!(s["idle_capacity"][0]["item"], "other:C");
    let no_session: Vec<_> = s["ready_owners"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["item"].as_str().unwrap(),
                r["no_live_session"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(no_session, [("demo:A", true), ("other:C", false)]);
    assert_eq!(s["ordered_items"].as_array().unwrap().len(), 2);
}

#[test]
fn unit_or_code_drift_sets_host_drift() {
    let mut unit = readings();
    unit["units"][0]["deployed_sha"] = json!("old");
    assert_eq!(collect(unit).0["host_drift"], true);
    let mut absent = readings();
    absent["units"][0]["deployed_sha"] = Value::Null;
    assert_eq!(collect(absent).0["host_drift"], true);
    let mut code = readings();
    code["hosts"][0]["origin_main_head"] = json!("bbb");
    code["hosts"][0]["behind_commits"] = json!(4);
    assert_eq!(collect(code).0["host_drift"], true);
}

#[test]
fn a_failed_reading_leaves_its_fields_out_and_the_checker_flags_it() {
    for (reading, lost) in [
        ("usage", vec!["pace", "budget"]),
        ("ci", vec!["ci_total", "ci_failed", "infra_failed"]),
        ("activity", vec!["closes_recent", "review_rounds"]),
        (
            "series",
            vec!["coding_vm_runs", "worker_idle", "idle_capacity"],
        ),
        ("critical_path", vec!["critical_idle_hours"]),
        ("units", vec!["host_drift"]),
    ] {
        let mut value = readings();
        value[reading] = Value::Null;
        let (snapshot, gaps) = collect(value);
        assert!(!gaps.is_empty(), "{reading} failure was not reported");
        for name in &lost {
            assert!(
                snapshot.get(name).is_none(),
                "{name} invented after {reading} failed"
            );
        }
        assert!(!check(&snapshot).0, "{reading} failure was accepted");
    }
}
