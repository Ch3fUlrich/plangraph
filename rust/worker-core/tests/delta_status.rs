use pg_worker_core::run::record::Status;

#[test]
fn needs_input_is_terminal() {
    let status = Status::parse("needs_input").expect("Python terminal status");
    assert!(status.is_terminal());
    assert_eq!(status.as_str(), "needs_input");
}
