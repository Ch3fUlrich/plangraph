use super::*;
fn outcome() -> OutcomeRow {
    serde_json::from_value(serde_json::json!({
        "leg_key":"leg","route_key":"route","ts":1,"role":"review",
        "outcome":"HANG_SILENT","run_id":"run-test","attempt":0,
        "duration_s":600,"reset_at":null,"reset_assumed":false,
        "reset_clamped":false,"open_tools_at_kill":null,"s_eff_s":600,"line":""
    }))
    .unwrap()
}
#[test]
fn redact_then_300_unicode_chars() {
    let mut row = outcome();
    row.line = format!(
        "{} sk-{} {}",
        "界".repeat(280),
        "x".repeat(80),
        "界".repeat(400)
    );
    let value = serde_json::to_value(&row).unwrap();
    let expected: String = pg_worker_core::redact::redact(&row.line)
        .chars()
        .take(300)
        .collect();
    assert_eq!(value["line"], expected);
    assert_eq!(expected.chars().count(), 300);
    assert!(expected.contains("[REDACTED]"));
}
#[test]
fn blind_kill_null_600_roundtrip() {
    let row = outcome();
    let value = serde_json::to_value(&row).unwrap();
    assert!(value["open_tools_at_kill"].is_null());
    assert_eq!(value["s_eff_s"], 600);
    assert_eq!(serde_json::from_value::<OutcomeRow>(value).unwrap(), row);
}
#[test]
fn raw_reset_and_tools_counts_roundtrip() {
    for count in [0, 1, 42] {
        let mut row = outcome();
        row.reset_at = Some(-123);
        row.reset_assumed = true;
        row.reset_clamped = true;
        row.open_tools_at_kill = Some(count);
        let value = serde_json::to_value(&row).unwrap();
        assert_eq!(value["reset_at"], -123);
        assert_eq!(value["open_tools_at_kill"], count);
        assert_eq!(serde_json::from_value::<OutcomeRow>(value).unwrap(), row);
    }
}
#[test]
fn legacy_leg_events_not_imported() {
    assert!(
        adapted(&serde_json::json!({"leg_events":[{"outcome":"OK"}]}))
            .0
            .is_empty()
    );
}
