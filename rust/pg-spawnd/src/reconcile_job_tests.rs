use super::*;
#[test]
fn exact_job_binding_only() {
    assert!(scope("run-test", "Local\\pg-run-test").is_ok());
    for name in [
        "Global\\pg-run-test",
        "Local\\pg-other",
        "Local\\pg-../run-test",
    ] {
        assert!(scope("run-test", name).is_err());
    }
    assert!(scope("../escape", "Local\\pg-../escape").is_err());
}
#[test]
fn absent_start_and_groupless_records_refused() {
    for group in [None, Some("Local\\pg-run-test".to_string())] {
        assert!(Group::open(
            "run-test",
            Proc {
                pid: 42,
                start_time: None,
                group,
                pgid: None
            }
        )
        .is_err());
    }
}
