use std::{fs, process::Command};

fn active(host: serde_json::Value) -> String {
    let dir = tempfile::tempdir().unwrap();
    let snapshot = serde_json::json!({"pace":66,"budget":66,"ci_failed":0,
        "ci_total":10,"infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"dell_idle":false,
        "host_drift":false,"closes_recent":4,"closes_previous":4,"hosts":[host]});
    let input = dir.path().join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .args([input.as_os_str(), dir.path().as_os_str()])
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    fs::read_to_string(dir.path().join("active")).unwrap_or_default()
}

#[test]
fn disk_usage_is_strictly_above_85_on_either_filesystem() {
    for (tmp, root, fires) in [(85.0, 85.0, false), (85.1, 0.0, true), (0.0, 85.1, true)] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":tmp,
            "root_used_percent":root,"checkout_head":"a","origin_main_head":"a",
            "behind_commits":0,"age_hours":100}));
        assert_eq!(result.contains("host-disk"), fires, "{result}");
    }
}

#[test]
fn checkout_alerts_by_commit_count_or_age_only_when_behind() {
    for (head, commits, age, fires) in [
        ("a", 10, 24.0, false),
        ("a", 11, 0.0, true),
        ("a", 1, 24.1, true),
        ("a", 1, 48.0, true),
        ("b", 0, 48.0, false),
        ("a", 0, 48.0, false),
    ] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":0,
            "root_used_percent":0,"checkout_head":head,"origin_main_head":"b",
            "behind_commits":commits,"age_hours":age}));
        assert_eq!(result.contains("host-code"), fires, "{result}");
        if fires {
            assert!(result.contains("fixture") && result.contains("a") && result.contains("b"));
        }
    }
}

#[test]
fn cat_hook_test_crates_are_unix_only() {
    for source in [
        include_str!("flow_watchdog.rs"),
        include_str!("flow_watchdog_regressions.rs"),
    ] {
        assert!(source.starts_with("#![cfg(unix)]"));
    }
}
