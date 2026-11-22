use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

fn level(bucket: &str, risk: &str, extra: Value) -> Value {
    let mut card = json!({"bucket":bucket,"risk_band":risk,"estimate_tokens":1000,
        "has_tests":true,"fanout":0,"write_count":0,"check_kind":"change"});
    card.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .arg("effort-level")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(card.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn levels_and_floors_follow_versioned_profiles() {
    let profiles: Value = serde_json::from_str(include_str!("../effort_profiles.json")).unwrap();
    for (bucket, expected, floor) in [
        ("S0", "L0", 37.5),
        ("S1", "L1", 44.0),
        ("S2", "L2", 49.0),
        ("S3", "L3", 50.0),
        ("S4", "L4", 51.0),
    ] {
        assert_eq!(profiles["levels"][expected]["floor"], floor);
        let result = level(bucket, "low", json!({}));
        assert_eq!(result["level"], expected);
        assert_eq!(result["floor"], floor);
    }
    for (risk, floor) in [
        ("low", 37.5),
        ("normal", 44.0),
        ("high", 46.0),
        ("critical", 50.0),
    ] {
        assert_eq!(profiles["risk_floors"][risk], floor);
        assert_eq!(level("S0", risk, json!({}))["risk_floor"], floor);
    }
}

#[test]
fn features_and_planner_assignment_have_deterministic_effects() {
    for (extra, expected) in [
        (json!({"has_tests":false}), "L1"),
        (json!({"fanout":6}), "L3"),
        (json!({"write_count":6}), "L3"),
        (json!({"estimate_tokens":64_001}), "L4"),
        (json!({"level":"L4"}), "L4"),
    ] {
        assert_eq!(level("S0", "low", extra)["level"], expected);
    }
    let lowered = level("S0", "critical", json!({"level":"L1"}));
    assert_eq!(lowered["level"], "L3");
    assert_eq!(lowered["lowered"]["assigned"], "L1");
    assert!(lowered["lowered"]["reason"].is_string());
}

#[test]
fn every_task_class_is_closed() {
    for (bucket, prefix) in [("S0", "small"), ("S2", "medium"), ("S4", "large")] {
        for kind in ["change", "regression"] {
            assert_eq!(
                level(bucket, "low", json!({"check_kind":kind}))["task_class"],
                format!("{prefix}_{kind}")
            );
        }
    }
}
