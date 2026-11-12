use pg_worker_core::turn_budget::{alert_line, read_alerts, role, BudgetStatus, ROLE_BUDGETS};
use serde_json::json;
use std::fs;

fn status(wake: u64, hours: usize) -> BudgetStatus {
    serde_json::from_value(json!({"session":"build-L1-0429", "role":"build-L1",
        "session_id":"test-session", "wake_calls":wake,"hour_calls":hours,
        "calls":vec![1000; hours]}))
    .unwrap()
}

#[test]
fn exactly_one_alert_when_either_or_both_caps_are_exceeded() {
    assert_eq!(alert_line(&status(6, 60), 1000), None);
    assert_eq!(
        alert_line(&status(7, 2), 1000).as_deref(),
        Some("TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=2/60")
    );
    assert_eq!(
        alert_line(&status(1, 61), 1000).as_deref(),
        Some("TURN-BUDGET build-L1 build-L1-0429 wake=1/6 hour=61/60")
    );
    let line = alert_line(&status(7, 61), 1000).unwrap();
    assert_eq!(
        line,
        "TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=61/60"
    );
    assert_eq!(line.lines().count(), 1);
    assert_eq!(alert_line(&status(1, 61), 4600), None);
}

#[test]
fn injectable_reader_skips_bad_files_and_recomputes_expired_hours() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("status");
    fs::create_dir(&path).unwrap();
    fs::write(
        path.join("test-session.json"),
        json!({"turn_budget":status(7, 61)}).to_string(),
    )
    .unwrap();
    fs::write(path.join("bad.json"), "bad json").unwrap();
    fs::write(path.join("unrelated.json"), "{}").unwrap();
    assert_eq!(
        read_alerts(dir.path(), 4600),
        vec!["TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=0/60"]
    );
    let mut unsafe_status = status(8, 0);
    unsafe_status.session = "forged\nline".into();
    assert_eq!(alert_line(&unsafe_status, 1000), None);
}

#[test]
fn role_strips_only_the_numeric_counter_and_unknown_roles_get_initial_caps() {
    assert_eq!(role("build-L1-0429"), "build-L1");
    assert_eq!(role("build-L1"), "build-L1");
    assert_eq!(role("review-L2-latest"), "review-L2-latest");
    assert!(ROLE_BUDGETS
        .iter()
        .all(|(_, wake, hour)| (*wake, *hour) == (6, 60)));
    let mut unknown = status(7, 0);
    unknown.session = "custom-role-12".into();
    unknown.role = "custom-role".into();
    assert_eq!(
        alert_line(&unknown, 1000).as_deref(),
        Some("TURN-BUDGET custom-role custom-role-12 wake=7/6 hour=0/60")
    );
}
