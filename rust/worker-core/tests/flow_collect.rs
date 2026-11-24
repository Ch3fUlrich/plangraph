#![cfg(unix)]
//! The collector's snapshot is accepted by the flow-watchdog checker, and every
//! required field is load-bearing for that acceptance.
use pg_worker_core::flow_collect::{assemble_with_gaps, decode, ABSENT_INPUTS};
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
         "lease_until":iso(1800.0),"updated_at":iso(4000.0),"last_activity_at":iso(5400.0)},
        {"key":"demo:F","state":"in_review","owner":"build","merged":true,"updated_at":iso(7200.0)},
        {"key":"plangraph:T","state":"open","depends_on":["demo:A","demo:E","demo:D"]},
        {"key":"zzz:X","state":"claimed","owner":"x","claimed_by":"writer-9",
         "lease_until":iso(-3600.0),"updated_at":iso(10.0),"last_activity_at":iso(100.0 * 3600.0)}],
      "ready_keys": ["demo:A","other:C"],
      "activity": [
        {"ts":iso(3600.0),"key":"demo:D","type":"review_passed"},
        {"ts":iso(30.0 * 3600.0),"key":"demo:G","type":"closed_on_decision"},
        {"ts":iso(50.0 * 3600.0),"key":"demo:H","type":"answered"},
        {"ts":iso(50000.0),"key":"demo:F","type":"changes_requested"},
        {"ts":iso(9000.0),"key":"demo:F","type":"claimed"},
        {"ts":iso(2000.0),"key":"demo:F","type":"changes_requested"},
        {"ts":iso(1000.0),"key":"demo:F","type":"changes_requested"}],
      "critical_target": "plangraph:T",
      "hosts": [{"host":"box","tmp_used_percent":10.0,"root_used_percent":20.0,
                 "checkout_head":"aaa","origin_main_head":"aaa","behind_commits":0,"age_hours":0.0}],
      "units": [{"host":"box","name":"w.service","deployed_sha":"s1","expected_sha":"s1"}],
      "series": [{"host":"vm","ts":NOW,"runs":2,"ready":0,"reachable":true,"sessions":[]},
                 {"host":"worker","ts":NOW,"runs":0,"ready":3,"reachable":true,"sessions":["plan-0001"]}],
      "coding_hosts": ["vm"], "worker_hosts": ["worker"]
    })
}

fn collect(value: Value) -> (Value, Vec<String>) {
    let (readings, malformed) = decode(value).unwrap();
    let assembled = assemble_with_gaps(&readings, malformed);
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
    assert!((s["critical_idle_hours"].as_f64().unwrap() - 1.5).abs() < 1e-9);
    assert_eq!(
        s["ready_families"],
        json!({"build":["demo"],"plan":["other"]})
    );
    assert_eq!(
        s["lapsed_leases"][0],
        json!({"item":"demo:E","owner":"writer-1","age_minutes":30,"open_lane":true})
    );
    assert_eq!(s["merged_reviews"][0]["age_minutes"], 120);
    // Inputs without a reader are named, never reported as zero or empty.
    for name in [
        "ordered_items",
        "idle_capacity",
        "go_requests",
        "writer_activity",
    ] {
        assert!(s.get(name).is_none(), "{name} must not be invented");
        assert!(s["absent_inputs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a == name));
    }
    assert_eq!(
        s["absent_inputs"].as_array().unwrap().len(),
        ABSENT_INPUTS.len()
    );
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
        ("critical_target", vec!["critical_idle_hours"]),
        ("ready_keys", vec!["ready_families", "ready_owners"]),
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

#[test]
fn critical_idle_follows_the_stall_rule_in_both_directions() {
    // An unstarted open item waiting for 100 hours is not a stall.
    let mut waiting = readings();
    waiting["items"][0]["updated_at"] = json!(iso(100.0 * 3600.0));
    waiting["items"][4]["last_activity_at"] = json!(iso(60.0));
    assert!(collect(waiting).0["critical_idle_hours"].as_f64().unwrap() < 0.02);
    // A claimed item under a live lease that has been silent for 20 hours is one.
    let mut silent = readings();
    silent["items"][4]["lease_until"] = json!(iso(-3600.0));
    silent["items"][4]["last_activity_at"] = json!(iso(20.0 * 3600.0));
    assert_eq!(collect(silent).0["critical_idle_hours"], 20.0);
    // Missing keeper activity is a failed reading, never a healthy zero.
    let mut unread = readings();
    unread["items"][4]
        .as_object_mut()
        .unwrap()
        .remove("last_activity_at");
    let (snapshot, gaps) = collect(unread);
    assert!(snapshot.get("critical_idle_hours").is_none() && !gaps.is_empty());
}

#[test]
fn a_claim_without_a_lease_does_not_drop_the_stuck_rows() {
    let mut value = readings();
    value["items"][4]["lease_until"] = Value::Null;
    let (snapshot, gaps) = collect(value);
    assert!(gaps.is_empty(), "{gaps:?}");
    assert_eq!(snapshot["lapsed_leases"], json!([]));
    assert_eq!(snapshot["merged_reviews"][0]["item"], "demo:F");
}

#[test]
fn an_empty_or_stale_series_is_a_gap_not_zero_runs_or_an_idle_worker() {
    let mut empty = readings();
    empty["series"] = json!([]);
    let (snapshot, gaps) = collect(empty);
    assert!(snapshot.get("coding_vm_runs").is_none() && snapshot.get("worker_idle").is_none());
    assert!(snapshot.get("ready_owners").is_none());
    assert!(
        gaps.iter().any(|g| g.contains("no fleet sample for vm")),
        "{gaps:?}"
    );
    assert!(!check(&snapshot).0);

    let mut stale = readings();
    stale["series"][1]["ts"] = json!(NOW - 3600.0);
    let (snapshot, gaps) = collect(stale);
    assert!(
        snapshot.get("worker_idle").is_none(),
        "a stale sample reported idleness"
    );
    assert!(gaps.iter().any(|g| g.contains("stale")), "{gaps:?}");

    let mut down = readings();
    down["series"][0]["reachable"] = json!(false);
    assert!(collect(down).0.get("coding_vm_runs").is_none());
    let mut worker_down = readings();
    worker_down["series"][1]["reachable"] = json!(false);
    let (snapshot, gaps) = collect(worker_down);
    assert_eq!(
        (snapshot["worker_idle"].clone(), gaps.is_empty()),
        (json!(false), true)
    );
}

#[test]
fn a_malformed_source_is_isolated_and_the_checker_sees_the_gap() {
    let mut value = readings();
    value["items"] = json!("not a list");
    value["ci"] = json!([{"conclusion": 5}]);
    let (snapshot, gaps) = collect(value);
    assert!(
        gaps.iter().any(|g| g.starts_with("items: malformed")),
        "{gaps:?}"
    );
    assert!(
        gaps.iter().any(|g| g.starts_with("ci: malformed")),
        "{gaps:?}"
    );
    assert!(
        snapshot.get("pace").is_some(),
        "other sources must still be read"
    );
    assert!(snapshot["collector_gaps"].as_array().unwrap().len() >= 2);
    assert!(!check(&snapshot).0);
}

#[test]
fn a_gap_in_an_optional_block_alone_still_reads_as_an_invalid_snapshot() {
    let mut value = readings();
    value["ready_keys"] = Value::Null;
    let (snapshot, gaps) = collect(value);
    assert!(!gaps.is_empty());
    for name in REQUIRED {
        assert!(snapshot.get(name).is_some(), "{name} is still collected");
    }
    assert!(
        !check(&snapshot).0,
        "the checker accepted a snapshot with a stuck-work gap"
    );
}

#[test]
fn critical_idle_follows_the_targets_open_dependencies_only() {
    // The silent claimed item outside the target's dependencies (zzz:X, 100 h) is not counted.
    let (snapshot, _) = collect(readings());
    assert!(snapshot["critical_idle_hours"].as_f64().unwrap() < 2.0);
    let mut inside = readings();
    inside["items"][7]["state"] = json!("claimed");
    inside["items"][6]["depends_on"] = json!(["demo:A", "demo:E", "zzz:X"]);
    assert_eq!(collect(inside).0["critical_idle_hours"], 100.0);
    let mut closed = readings();
    closed["items"][4]["state"] = json!("done");
    assert_eq!(collect(closed).0["critical_idle_hours"], 0.0);
    let mut gone = readings();
    gone["critical_target"] = json!("plangraph:NOPE");
    assert!(collect(gone).0.get("critical_idle_hours").is_none());
    let mut missing_dep = readings();
    missing_dep["items"][6]["depends_on"] = json!(["demo:E", "nowhere:1"]);
    assert!(collect(missing_dep).0.get("critical_idle_hours").is_none());
}

#[test]
fn review_rounds_start_again_at_a_new_claim() {
    let (snapshot, _) = collect(readings());
    // Two requests since the latest claim of demo:F count, the one before it does not.
    assert_eq!(snapshot["review_rounds"], 3);
    let mut fresh = readings();
    fresh["activity"].as_array_mut().unwrap().push(json!(
        {"ts":iso(500.0),"key":"demo:F","type":"claimed"}));
    assert_eq!(collect(fresh).0["review_rounds"], 1);
}
