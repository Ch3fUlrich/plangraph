use super::*;

#[test]
fn native_writer_stop_during_fast_checks_preserves_the_session_without_another_fix_turn() {
    for fast_failures in [0, FAST_CHECK_FIXES] {
        let mut driver = Fake {
            fast_failures,
            stop_on_fast_checks: true,
            ..fake()
        };
        let record = run(&mut driver, json!({}), "brief");
        assert_eq!(record["status"], "stopped");
        assert_eq!(record["exit_code"], 10);
        assert_eq!(record["reason"], crate::writer_control::STOPPED);
        assert_eq!(record["session_id"], "same-session");
        assert!(record["result"].is_null());
        assert_ne!(
            record["fast_checks"]["output"],
            crate::writer_control::STOPPED
        );
        assert_eq!(driver.calls.len(), fast_failures + 1);
        assert!(driver.reviews.is_empty());
        assert!(driver.pushed.is_empty());
    }
}

#[test]
fn native_writer_stop_during_base_refresh_is_not_blocked() {
    let mut driver = Fake {
        failures: 1,
        stop_on_refresh: true,
        ..fake()
    };
    let record = run(
        &mut driver,
        json!({"landing_resume": true, "session_id": "saved-session"}),
        "brief",
    );
    assert_eq!(record["status"], "stopped");
    assert_eq!(record["exit_code"], 10);
    assert_eq!(record["reason"], crate::writer_control::STOPPED);
    assert_eq!(record["session_id"], "saved-session");
    assert!(record["result"].is_null());
    assert_eq!(driver.refreshes, 1);
    assert_eq!(driver.calls, vec![Some("saved-session".into())]);
}
