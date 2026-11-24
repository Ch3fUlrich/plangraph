use super::*;

#[test]
fn usage_limit_turn_pushes_the_committed_head_without_relaunching() {
    // The first turn commits, then a later turn of the same run hits the limit.
    let mut driver = Fake {
        quota_from: Some(2),
        verdicts: vec![("REQUEST_CHANGES", 1)],
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(driver.calls.len(), 2, "no relaunch after the quota turn");
    assert_eq!(record["status"], "failed", "{record}");
    assert_eq!(record["exit_code"], 11);
    assert_eq!(record["result"], "QUOTA");
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .starts_with("quota_limited:"));
    assert_eq!(record["quota_limited"]["reset"], "Nov 27th, 2026 8:24 PM");
    assert!(record["status_line"]
        .as_str()
        .unwrap()
        .starts_with("QUOTA 2026-11-2"));
    assert_eq!(driver.quota_booked.len(), 1);
    assert_eq!(driver.pushed, ["final-head-2"]);
    assert_eq!(record["pushed"], "refs/heads/lane/fixture");
    assert_eq!(record["final_head"], "final-head-2");
    assert_eq!(driver.refreshes, 0);
}

#[test]
fn usage_limit_on_the_first_turn_pushes_nothing() {
    let mut driver = Fake {
        quota_from: Some(1),
        uncommitted_turns: 1,
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(driver.calls.len(), 1);
    assert_eq!(record["exit_code"], 11);
    assert!(driver.pushed.is_empty());
    assert_eq!(driver.quota_booked.len(), 1);
    assert!(record["status_line"]
        .as_str()
        .unwrap()
        .starts_with("QUOTA "));
}

#[test]
fn usage_limit_with_a_refused_push_names_the_head() {
    let mut driver = Fake {
        quota_from: Some(2),
        verdicts: vec![("REQUEST_CHANGES", 1)],
        push_failures: 1,
        ..fake()
    };

    let record = run(&mut driver, json!({}), "brief");

    assert_eq!(record["exit_code"], 11);
    let reason = record["reason"].as_str().unwrap();
    assert!(reason.contains("push of final-head-2 refused"), "{reason}");
    assert!(reason.contains("pre-push: fixture scrub hit"), "{reason}");
    assert_eq!(record["final_head"], "final-head-2");
    assert!(record["pushed"].is_null());
}

#[test]
fn usage_limit_on_a_saved_session_resume_is_a_quota_stop() {
    let mut driver = Fake {
        quota_from: Some(1),
        head_script: vec!["final-head"],
        ..fake()
    };

    let record = run_resume(&mut driver, json!({"base_sha":"base"}), "saved", "go on");

    assert_eq!(record["exit_code"], 11);
    assert_eq!(driver.pushed, ["final-head"]);
    assert_eq!(driver.quota_booked.len(), 1);
}

#[test]
fn a_resumed_quota_stop_completes_without_the_stop_fields() {
    let mut driver = Fake {
        quota_from: Some(1),
        ..fake()
    };
    let mut stopped = run(&mut driver, json!({}), "brief");
    assert!(stopped["quota_limited"].is_object());
    assert_eq!(stopped["quota_record_error"], serde_json::Value::Null);
    stopped["quota_record_error"] = json!(true);
    // The reopen path of `pg-spawnd resume` clears the stop before the run restarts.
    for key in ["ended", "exit_code", "reason", "result", "status_line"] {
        stopped.as_object_mut().unwrap().remove(key);
    }
    quota::clear(&mut stopped);
    stopped["session_id"] = json!("same-session");
    let mut driver = fake();
    let record = run_resume(&mut driver, stopped, "same-session", "go on");
    assert_eq!(record["status"], "completed", "{record}");
    assert!(record["quota_limited"].is_null() && record["quota_record_error"].is_null());
    assert_eq!(record["status_line"], "DONE");
}
