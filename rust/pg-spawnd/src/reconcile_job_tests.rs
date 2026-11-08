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
        assert!(validate(
            "run-test",
            &Proc {
                pid: 42,
                start_time: None,
                group,
                pgid: None
            }
        )
        .is_err());
    }
}
#[test]
fn reconcile_job_zero_identity_refused() {
    for (pid, start_time) in [(0, Some(99)), (42, Some(0)), (42, None)] {
        assert!(validate(
            "run-test",
            &Proc {
                pid,
                start_time,
                group: Some("Local\\pg-run-test".into()),
                pgid: None,
            }
        )
        .is_err());
    }
    assert!(validate(
        "run-test",
        &Proc {
            pid: 42,
            start_time: Some(99),
            group: Some("Local\\pg-run-test".into()),
            pgid: None,
        }
    )
    .is_ok());
}
