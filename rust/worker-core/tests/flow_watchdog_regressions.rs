use std::{fs, path::Path, process::Command};
fn safe() -> serde_json::Value {
    serde_json::json!({"pace":66,"budget":66,"ci_failed":1,"ci_total":10,
        "infra_failed":0,"review_age_hours":24,"review_rounds":3,
        "critical_idle_hours":12,"coding_vm_runs":0,"dell_idle":true,
        "host_drift":false,"closes_recent":4,"closes_previous":4})
}
fn run(s: &serde_json::Value, dir: &Path, push: bool) -> std::process::Output {
    fs::write(dir.join("input.json"), s.to_string()).unwrap();
    Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(dir.join("input.json")).arg(dir)
        .env("PLANGRAPH_WATCHDOG_DELIVERY", "[\"/bin/cat\"]")
        .env("PLANGRAPH_FLOW_PUSH", if push { "[\"/bin/cat\"]" } else { "[\"/bin/false\"]" })
        .output().unwrap()
}
#[test]
fn stale_markers_cannot_ack_a_new_event() {
    let d = tempfile::tempdir().unwrap();
    // A directory forces marker reset to fail, simulating interruption before reset.
    fs::create_dir(d.path().join("routing.ok")).unwrap();
    fs::write(d.path().join("push.ok"), "ok").unwrap();
    let mut s = safe(); s["host_drift"] = true.into();
    assert!(!run(&s, d.path(), true).status.success());
    assert!(!d.path().join("pending.json").exists());
}
#[test]
fn failed_push_does_not_freeze_clear_and_refire() {
    let d = tempfile::tempdir().unwrap();
    let mut s = safe(); s["host_drift"] = true.into();
    assert!(run(&s, d.path(), false).status.success());
    assert!(run(&safe(), d.path(), false).status.success());
    assert_eq!(fs::read_to_string(d.path().join("active")).unwrap(), "");
    assert!(run(&s, d.path(), false).status.success());
    assert!(run(&s, d.path(), true).status.success());
    assert!(!d.path().join("pending.json").exists());
    let queue: Vec<serde_json::Value> = serde_json::from_str(
        &fs::read_to_string(d.path().join("queue.json")).unwrap()).unwrap();
    assert!(queue.is_empty());
}
#[test]
fn zero_negative_and_missing_budget_are_not_healthy() {
    for budget in [0, -1] {
        let d = tempfile::tempdir().unwrap(); let mut s = safe(); s["budget"] = budget.into();
        assert!(!run(&s, d.path(), true).status.success());
    }
    let d = tempfile::tempdir().unwrap(); let mut s = safe();
    s.as_object_mut().unwrap().remove("budget");
    assert!(!run(&s, d.path(), true).status.success());
}
#[test]
fn inconsistent_counts_are_rejected_without_state() {
    for (failed, infra) in [(11, 0), (1, 2)] {
        let d = tempfile::tempdir().unwrap(); let mut s = safe();
        s["ci_failed"] = failed.into(); s["infra_failed"] = infra.into();
        let out = run(&s, d.path(), true);
        assert!(!out.status.success());
        assert!(String::from_utf8_lossy(&out.stderr).contains("inconsistent CI counts"));
        assert!(!d.path().join("active").exists());
    }
}
#[test]
fn every_rule_fires_in_isolation_and_retries_failed_hook() {
    for (field, value, rule) in [
        ("pace", serde_json::json!(67), "claude-pace"),
        ("ci_failed", serde_json::json!(2), "ci-fail-rate"),
        ("review_age_hours", serde_json::json!(25), "review-age"),
        ("review_rounds", serde_json::json!(4), "review-rounds"),
        ("critical_idle_hours", serde_json::json!(13), "critical-path-idle"),
        ("coding_vm_runs", serde_json::json!(1), "placement"),
        ("host_drift", serde_json::json!(true), "host-drift"),
        ("closes_recent", serde_json::json!(3), "closes-trend")] {
        let d = tempfile::tempdir().unwrap(); let mut s = safe(); s[field] = value;
        assert!(run(&safe(), d.path(), true).status.success());
        assert!(run(&s, d.path(), false).status.success());
        let pending = fs::read_to_string(d.path().join("pending.json")).unwrap();
        let event: serde_json::Value = serde_json::from_str(&pending).unwrap();
        assert_eq!(event["active"], rule);
        assert!(d.path().join("routing.ok").exists());
        assert!(!d.path().join("push.ok").exists());
        fs::write(d.path().join("routing.ok"), "checkpoint retained").unwrap();
        assert!(run(&s, d.path(), true).status.success());
        assert!(!d.path().join("pending.json").exists());
        assert_eq!(fs::read_to_string(d.path().join("routing.ok")).unwrap(),
            "checkpoint retained");
    }
}
