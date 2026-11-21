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

#[test]
fn ruled_review_leg_alerts_once_after_an_hour_of_reviews_elsewhere() {
    let dir = tempfile::tempdir().unwrap();
    let mut snapshot = serde_json::json!({"pace":66.0,"budget":66.0,
        "ci_failed":1,"ci_total":10,"infra_failed":0,
        "review_age_hours":24.0,"review_rounds":3,"critical_idle_hours":12.0,
        "coding_vm_runs":0,"worker_idle":false,"host_drift":false,
        "closes_recent":4,"closes_previous":4,
        "review_leg_activity":{"window_seconds":3599,
            "first_leg":"agy-gemini-4-hydrogen-high","served":{"neon":5}}});
    assert!(run(&snapshot, dir.path()).status.success());
    let pending = dir.path().join("pending.json");
    assert!(!pending.exists());

    snapshot["review_leg_activity"]["window_seconds"] = 3600.into();
    assert!(run(&snapshot, dir.path()).status.success());
    let first = fs::read_to_string(&pending).unwrap();
    let event: serde_json::Value = serde_json::from_str(&first).unwrap();
    let message = event["message"].as_str().unwrap();
    assert_eq!(message.lines().count(), 1);
    assert!(message.contains("review-leg-unserved leg=\"agy-gemini-4-hydrogen-high\""));

    snapshot["review_leg_activity"]["served"]["neon"] = 6.into();
    assert!(run(&snapshot, dir.path()).status.success());
    assert_eq!(fs::read_to_string(&pending).unwrap(), first);
    snapshot["review_leg_activity"]["served"]["agy-gemini-4-hydrogen-high"] = 1.into();
    assert!(run(&snapshot, dir.path()).status.success());
    assert_eq!(fs::read_to_string(dir.path().join("active")).unwrap(), "");
}

#[test]
fn idle_writers_alert_for_open_work_and_use_the_status_path_at_launch() {
    let dir = tempfile::tempdir().unwrap();
    let writer = |id: &str, busy, review: serde_json::Value, status_path: &str, line: &str| {
        serde_json::json!({"host":"fixture-host","session_id":id,"parent":"fixture-parent",
            "busy":busy,"idle_since":0,"head":"fixture-head","last_commit":60,
            "review":review,"status_path":"/fixture/expected.status",
            "status":{"path":status_path,"last_line":line}})
    };
    let expected = "/fixture/expected.status";
    let accepted = serde_json::json!({"head":"fixture-head","verdict":"ACCEPT"});
    let changes = serde_json::json!({"head":"fixture-head","verdict":"REQUEST_CHANGES"});
    let mut snapshot = serde_json::json!({"pace":66.0,"budget":66.0,
    "ci_failed":1,"ci_total":10,"infra_failed":0,
    "review_age_hours":24.0,"review_rounds":3,"critical_idle_hours":12.0,
    "coding_vm_runs":0,"worker_idle":false,"host_drift":false,
    "closes_recent":4,"closes_previous":4,
    "writer_activity":{"now":1199,"writers":[
        writer("changes", false, changes.clone(), expected, ""),
        writer("unreviewed", false, serde_json::json!({"head":"earlier-head","verdict":"ACCEPT"}), expected, ""),
        writer("wrong-status", false, accepted.clone(), "/fixture/old.status", "READY"),
        writer("working", true, changes, expected, ""),
        writer("ready", false, accepted, expected, "READY fixture result"),
    ]}});
    let pending = dir.path().join("pending.json");
    assert!(run(&snapshot, dir.path()).status.success());
    assert!(!pending.exists());
    snapshot["writer_activity"]["now"] = 1200.into();
    assert!(run(&snapshot, dir.path()).status.success());
    let queue = dir.path().join("queue.json");
    let original = fs::read_to_string(&queue).unwrap();
    let events: Vec<serde_json::Value> = serde_json::from_str(&original).unwrap();
    assert_eq!(events.len(), 3, "{events:?}");
    for (id, reason) in [
        ("changes", "request_changes"),
        ("unreviewed", "unreviewed_head"),
        ("wrong-status", "missing_status"),
    ] {
        assert!(events.iter().any(|event| {
            let line = event["message"].as_str().unwrap();
            line.lines().count() == 1
                && line.contains(&format!("session=\"{id}\""))
                && line.contains(&format!("reason={reason}"))
                && event["recipient"] == "fixture-parent"
        }));
    }
    snapshot["writer_activity"]["now"] = 1260.into();
    assert!(run(&snapshot, dir.path()).status.success());
    assert_eq!(fs::read_to_string(queue).unwrap(), original);
}
