use super::*;

#[test]
fn finish_reports_malformed_attempts_without_dropping_them() {
    let dir = std::env::temp_dir().join(new_run_id());
    let mut record = create(
        &dir,
        "run-invalid",
        "fixture",
        json!({"attempts":{"invalid":true}}),
        Status::Running,
    )
    .unwrap_or_else(|_| panic!("fixture record creation failed"));
    finish(&mut record, Status::Failed, &json!({}), 1);
    assert_eq!(record.get("attempts"), Some(&json!({"invalid":true})));
    assert_eq!(
        record.get("attempts_error"),
        Some(&json!("attempts must be an array"))
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn finish_preserves_proc_and_outcome() {
    let dir = std::env::temp_dir().join(new_run_id());
    let prior = json!({"status":"failed", "outcome":{"how":"prior"}});
    let proc = json!({"pid":42,"start_time":17,"group":null});
    let outcome = json!({"how":"fixture"});
    let mut record = create(
        &dir,
        "run-bridge",
        "fixture",
        json!({
            "proc":proc,"started":12,"max_s":1800,
            "attempts":[prior,{"outcome":outcome,"custom":true,"started":13,"proc":proc,"max_s":1800}]
        }),
        Status::Running,
    )
    .unwrap_or_else(|_| panic!("fixture record creation failed"));
    finish(
        &mut record,
        Status::Failed,
        &json!({"model":"fixture","family":"fixture"}),
        2,
    );
    let attempts = record.get("attempts").unwrap().as_array().unwrap();
    assert_eq!(attempts.len(), 2);
    assert_eq!(attempts[0], prior);
    assert_eq!(attempts[1]["proc"], proc);
    assert_eq!(attempts[1]["started"], 13);
    assert_eq!(attempts[1]["max_s"], 1800);
    assert_eq!(attempts[1]["outcome"], outcome);
    assert_eq!(attempts[1]["custom"], true);
    assert_eq!(attempts[1]["status"], "failed");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn finish_settles_non_last_attempt_and_rejects_unknown_ids() {
    let dir = std::env::temp_dir().join(new_run_id());
    let first = json!({"status":"running","exit_code":7,"custom":"first"});
    let last = json!({"status":"running","exit_code":19,"custom":"last"});
    let mut record = create(
        &dir,
        "run-orphan",
        "fixture",
        json!({"attempts":[first,last]}),
        Status::Running,
    )
    .unwrap_or_else(|_| panic!("fixture record creation failed"));
    finish(&mut record, Status::Orphaned, &json!({"exit_code":3}), 1);
    let attempts = record.get("attempts").unwrap().clone();
    assert_eq!(attempts[0]["status"], "orphaned");
    assert_eq!(attempts[0]["exit_code"], 7);
    assert_eq!(attempts[0]["custom"], "first");
    assert_eq!(attempts[1], last);
    for id in [0, 3] {
        finish(&mut record, Status::Failed, &json!({}), id);
        assert_eq!(record.get("attempts"), Some(&attempts));
        assert_eq!(
            record.get("attempts_error"),
            Some(&json!("unknown or invalid attempt id"))
        );
    }
    finish(&mut record, Status::Failed, &json!({}), 1);
    assert_eq!(record.get("attempts_error"), Some(&Value::Null));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn finish_rejects_empty_and_non_object_entries_without_appending() {
    let dir = std::env::temp_dir().join(new_run_id());
    for attempts in [json!([]), json!([null]), json!([7])] {
        let mut record = create(
            &dir,
            "run-invalid",
            "fixture",
            json!({"attempts":attempts}),
            Status::Running,
        )
        .unwrap_or_else(|_| panic!("fixture record creation failed"));
        finish(&mut record, Status::Failed, &json!({}), 1);
        assert_eq!(record.get("attempts"), Some(&attempts));
        assert_eq!(
            record.get("attempts_error"),
            Some(&json!("unknown or invalid attempt id"))
        );
        // The supervisor owns the terminal transition after the finish hook.
        record.transition(Status::Failed, 10.0).unwrap();
        assert_eq!(record.status(), Some(Status::Failed));
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn orphan_settle_selects_non_last_non_first_attempt() {
    let dir = std::env::temp_dir().join(new_run_id());
    let first = json!({"status":"failed","exit_code":3});
    let last = json!({"status":"running","exit_code":19});
    let mut record = create(
        &dir,
        "run-orphan",
        "fixture",
        json!({
            "role":"implement", "attempts":[first,
                {"status":"running","started":1,"exit_code":7,
                 "leg":{"leg_key":"leg","route_key":"route"}}, last]
        }),
        Status::Running,
    )
    .unwrap_or_else(|_| panic!("fixture record creation failed"));
    crate::reconcile_store::settle(&dir, &mut record, 2, "gone", 2000).unwrap();
    let saved = RunRecord::read(&dir, "run-orphan").unwrap();
    let attempts = saved.get("attempts").unwrap();
    assert_eq!(attempts[0], first);
    assert_eq!(attempts[1]["status"], "orphaned");
    assert_eq!(attempts[1]["exit_code"], Value::Null);
    assert_eq!(attempts[2], last);
    assert_eq!(saved.status(), Some(Status::Orphaned));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn empty_prompt_and_metadata() {
    let dir = std::env::temp_dir().join(new_run_id());
    let secret = format!("sk-{}", "a".repeat(24));
    let mut launch = crate::launch::LaunchSpec {
        argv: vec!["fake".into()],
        cwd: dir.clone(),
        env: BTreeMap::new(),
        stdin: None,
        prompt: Some(String::new()),
        warnings: Some(vec![format!("warning {secret}")]),
        events: Some(vec![json!({"message":secret}).as_object().unwrap().clone()]),
    };
    let id = new_run_id();
    assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_ok());
    assert_eq!(launch.argv, ["fake"]);
    assert_eq!(launch.prompt.as_deref(), Some(""));
    assert_eq!(
        std::fs::read(dir.join("runs").join(id).join("prompt.txt")).unwrap(),
        b""
    );
    assert!(!serde_json::to_string(&launch.warnings)
        .unwrap()
        .contains(&secret));
    assert!(!serde_json::to_string(&launch.events)
        .unwrap()
        .contains(&secret));
    std::fs::remove_dir_all(dir).unwrap();
}
