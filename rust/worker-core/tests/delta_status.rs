use pg_worker_core::run::record::Status;

#[test]
fn needs_input_is_terminal() {
    let status = Status::parse("needs_input").expect("Python terminal status");
    assert!(status.is_terminal());
    assert_eq!(status.as_str(), "needs_input");
}

#[test]
fn queued_can_need_input() {
    use pg_worker_core::run::record::RunRecord;
    let state = tempfile::tempdir().unwrap();
    let mut record = RunRecord::create(
        state.path(),
        "run-test",
        "test",
        serde_json::Map::new(),
        Status::Queued,
        0.0,
    )
    .unwrap();
    record.transition(Status::NeedsInput, 1.0).unwrap();
    assert_eq!(record.status(), Some(Status::NeedsInput));
    assert_eq!(record.get("ended"), Some(&serde_json::json!(1.0)));
    assert!(record.transition(Status::Running, 2.0).is_err());
}
