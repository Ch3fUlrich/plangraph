use super::*;
use serde_json::json;

fn args(value: Value) -> Args {
    value.as_object().unwrap().clone()
}

fn live_run(state: &Path) {
    let run = state.join("runs/run-a");
    std::fs::create_dir_all(&run).unwrap();
    let record = json!({"run_id":"run-a","run_kind":"native-codex","status":"queued"});
    std::fs::write(run.join("record.json"), record.to_string()).unwrap();
    let request = json!({"prompt":"Item key: plangraph:Z-1\nNAME: lane-one\n"});
    std::fs::write(run.join("native-request.json"), request.to_string()).unwrap();
}

#[test]
fn native_submit_refuses_a_held_item_naming_the_lane() {
    let dir = tempfile::tempdir().unwrap();
    live_run(dir.path());
    let sessions = dir.path().join("sessions");
    let submit = args(json!({"card":{"item_key":"plangraph:Z-1"}}));
    let ToolError::Refused { message, .. } =
        guard_lanes(dir.path(), &sessions, &submit, "plain prompt").unwrap_err()
    else {
        panic!("expected a refusal");
    };
    assert!(
        message.contains("plangraph:Z-1 is held by native run lane-one (run-a)"),
        "{message}"
    );
    let free = args(json!({"card":{"item_key":"plangraph:Z-2"}}));
    assert!(guard_lanes(dir.path(), &sessions, &free, "x").is_ok());
}

#[test]
fn native_submit_override_needs_a_reason_and_records_it() {
    let dir = tempfile::tempdir().unwrap();
    live_run(dir.path());
    let sessions = dir.path().join("sessions");
    let submit =
        |reason: Value| args(json!({"card":{"item_key":"plangraph:Z-1"},"lane_override":reason}));
    assert!(guard_lanes(dir.path(), &sessions, &submit(json!("")), "p").is_err());
    assert!(guard_lanes(dir.path(), &sessions, &submit(json!(7)), "p").is_err());
    let allowed = submit(json!("lane-one is wedged"));
    assert!(guard_lanes(dir.path(), &sessions, &allowed, "p").is_ok());
    let log = std::fs::read_to_string(dir.path().join("lane-overrides.jsonl")).unwrap();
    assert!(
        log.contains("lane-one is wedged") && log.contains("native-submit"),
        "{log}"
    );
}
