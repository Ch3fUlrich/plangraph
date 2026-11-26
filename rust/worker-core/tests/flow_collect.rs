//! Checks the collection rules against the unchanged watchdog executable.
use pg_worker_core::flow_collect::assemble;
use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

const NOW: u64 = 1_800_000_000;
const REQUIRED: &[&str] = &[
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

fn readings() -> Value {
    json!({
        "now": NOW,
        "usage":{"weighted":1.0,"budget_per_day":2.0}, "ci": [],
        "keeper":{"items":[
            {"key":"demo:T","state":"open","epoch":0,"depends_on":["demo:R"]},
            {"key":"demo:R","state":"in_review","epoch":7,"owner":"review","depends_on":[],
             "updated_at":"2027-01-15T07:00:00Z"},
            {"key":"demo:A","state":"open","epoch":0,"owner":"dev","depends_on":[],
             "updated_at":"2027-01-15T07:00:00Z"}
        ],"activity":[]},
        "item_details":{"demo:R":{"key":"demo:R","epoch":7,"round":2,"state":"in_review",
                                  "last_activity":{"at":"2027-01-15T07:00:00Z"}}},
        "ready":{"items":[{"key":"demo:A"}]},
        "critical_target":"demo:T",
        "hosts":[{"host":"vm","checkout_head":"abc","origin_main_head":"abc",
                  "behind_commits":0,"age_hours":0.0,"disks":[]}],
        "units": [],
        "series":[
            {"host":"vm","ts":NOW,"runs":2,"ready":0,"reachable":true,"sessions":[],"cores":4,"sessions_lost":[],"top_rss":[]},
            {"host":"worker","ts":NOW,"runs":0,"ready":1,"reachable":true,"sessions":[],"cores":4,"sessions_lost":[],"top_rss":[]},
        ],
        "coding_hosts":["vm"], "worker_hosts":["worker"]
    })
}

fn watchdog(dir: &Path, snapshot: &Value, valid: bool) -> String {
    let input = dir.join("snapshot.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(&input)
        .arg(dir.join("state"))
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_TURN_BUDGET_STATE_DIR")
        .env_remove("PLANGRAPH_WATCHDOG_PROC_ROOT")
        .output()
        .unwrap();
    assert_eq!(
        out.status.success(),
        valid,
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stderr).unwrap()
}

#[test]
fn watchdog_rejects_each_missing_required_field() {
    let assembled = assemble(&readings()).unwrap();
    assert!(
        assembled.required_errors.is_empty(),
        "{:?}",
        assembled.required_errors
    );
    let snapshot = assembled.snapshot;
    let dir = tempfile::tempdir().unwrap();
    assert!(!watchdog(dir.path(), &snapshot, true).contains("invalid snapshot"));
    for field in REQUIRED {
        let mut broken = snapshot.clone();
        assert!(
            broken.as_object_mut().unwrap().remove(*field).is_some(),
            "{field}"
        );
        let test_dir = tempfile::tempdir().unwrap();
        let stderr = watchdog(test_dir.path(), &broken, false);
        assert!(
            stderr.contains(&format!("missing field `{field}`")),
            "{stderr}"
        );
        let active = fs::read_to_string(test_dir.path().join("state/active")).unwrap();
        assert_eq!(active.trim(), "invalid snapshot");
    }
}

#[test]
fn bounded_samples_and_current_review_epoch() {
    let mut input = readings();
    let result = assemble(&input).unwrap();
    assert_eq!(result.snapshot["review_rounds"], 2);
    assert_eq!(result.snapshot["worker_idle"], true);
    for ts in [NOW - 301, NOW + 301] {
        input["series"][1]["ts"] = json!(ts);
        let stale = assemble(&input).unwrap();
        assert!(stale.required_errors.contains_key("placement"));
        assert!(stale.snapshot.get("worker_idle").is_none());
        assert!(stale.snapshot.get("ready_owners").is_none());
    }
    input["series"][1]["ts"] = json!(NOW);
    input["keeper"]["items"][1]["epoch"] = json!(8);
    let reopened = assemble(&input).unwrap();
    assert!(reopened.required_errors.contains_key("review"));
    assert!(reopened.snapshot.get("review_rounds").is_none());
    input["item_details"]["demo:R"]["epoch"] = json!(8);
    input["item_details"]["demo:R"]["round"] = json!(1);
    assert_eq!(assemble(&input).unwrap().snapshot["review_rounds"], 1);
}

#[test]
fn idle_capacity_needs_separate_current_resources() {
    let mut input = readings();
    let snapshot = assemble(&input).unwrap().snapshot;
    assert!(snapshot.get("host_idle_capacity").is_none());
    assert!(snapshot["unavailable_inputs"]["host_idle_capacity"].is_string());
    input["host_idle_capacity"] = json!({"observed_at": NOW, "snapshot":{
        "hosts":[{"host":"worker","caps":{"max_runs":8,"max_writers":4,"max_seats":4,
            "max_sessions":2,"mem_floor_mb":3000},"table":{"version":1,"active":[],"queued":[]},
            "rss_mb":{},"mem_available_mb":26000,"writer_mb":2048,"disk_free_bytes":20000000000u64,
            "cpus":32,"load1":3.0}],
        "ready_items":[{"key":"demo:A","ready":true,"blocked":false,"claimed":false}]
    }});
    let result = assemble(&input).unwrap();
    assert!(result.snapshot["host_idle_capacity"].is_object());
    let dir = tempfile::tempdir().unwrap();
    assert!(!watchdog(dir.path(), &result.snapshot, true).contains("invalid snapshot"));
    assert!(fs::read_to_string(dir.path().join("state/active"))
        .unwrap()
        .contains("host-idle worker"));
    input["host_idle_capacity"]["observed_at"] = json!(NOW - 301);
    assert!(assemble(&input)
        .unwrap()
        .snapshot
        .get("host_idle_capacity")
        .is_none());
}

#[test]
fn ci_counts_share_the_same_window() {
    let mut input = readings();
    input["ci"] = json!([
        {"conclusion":"success","created_at":"2027-01-15T07:59:59Z"},
        {"conclusion":"failure","created_at":"2027-01-08T08:00:00Z"},
        {"conclusion":"timed_out","created_at":"2027-01-15T07:59:59Z"},
        {"conclusion":"skipped","created_at":"2027-01-15T07:59:59Z"},
        {"conclusion":"startup_failure","created_at":"2027-01-08T07:59:59Z"},
        {"conclusion":"failure","created_at":"2027-01-15T08:00:01Z"}
    ]);
    let result = assemble(&input).unwrap();
    assert!(
        result.required_errors.is_empty(),
        "{:?}",
        result.required_errors
    );
    assert_eq!(result.snapshot["ci_total"], 3);
    assert_eq!(result.snapshot["ci_failed"], 2);
    assert_eq!(result.snapshot["infra_failed"], 1);
}

#[test]
fn disk_read_failure_does_not_discard_git_ancestry() {
    let mut input = readings();
    let host = input["hosts"][0].as_object_mut().unwrap();
    host.remove("disks");
    host.insert("disk_error".into(), json!("fixture disk unavailable"));
    host.insert("behind_commits".into(), json!(1));
    let result = assemble(&input).unwrap();
    assert!(
        result.required_errors.is_empty(),
        "{:?}",
        result.required_errors
    );
    assert_eq!(result.snapshot["host_drift"], true);
    assert!(result.snapshot.get("hosts").is_none());
    assert_eq!(
        result.snapshot["unavailable_inputs"]["hosts"],
        "disk observation for vm: fixture disk unavailable"
    );
}
