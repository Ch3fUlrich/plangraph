use super::*;
#[test]
fn legacy_leg_events_not_imported() {
    assert!(
        adapted(&serde_json::json!({"leg_events":[{"outcome":"OK"}]}))
            .0
            .is_empty()
    );
}
