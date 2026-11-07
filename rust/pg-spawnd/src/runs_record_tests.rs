use super::*;

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
            "attempts":[prior,{"outcome":outcome,"custom":true,"started":13}]
        }),
        Status::Running,
    )
    .unwrap_or_else(|_| panic!("fixture record creation failed"));
    finish(
        &mut record,
        Status::Failed,
        &json!({"model":"fixture","family":"fixture"}),
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
