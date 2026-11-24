use serde_json::{json, Value};

fn lane_state(role: &str) -> tempfile::TempDir {
    let state = tempfile::tempdir().unwrap();
    let run = state.path().join("runs/run-held");
    std::fs::create_dir_all(&run).unwrap();
    std::fs::write(
        run.join("record.json"),
        json!({"run_id":"run-held","status":"running","role":role,"item_key":"demo:HELD"})
            .to_string(),
    )
    .unwrap();
    state
}

fn lane_args(card_extra: Value) -> serde_json::Map<String, Value> {
    let mut card = json!({"run_kind":"native-codex","repo":"/repo-without-git"});
    card.as_object_mut()
        .unwrap()
        .extend(card_extra.as_object().unwrap().clone());
    json!({"card":card,"role":"implement","prompt":"Brief\n### demo:HELD | held\nWhy: x\n"})
        .as_object()
        .unwrap()
        .clone()
}

#[test]
fn native_dispatch_refuses_a_key_held_by_a_live_writer_lane_and_names_it() {
    let state = lane_state("implement");
    let refusal = |result: Result<_, crate::errors::ToolError>| {
        result
            .expect_err("held key refused")
            .into_error_data()
            .message
            .to_string()
    };
    let error =
        refusal(crate::placement::admit_lane(state.path(), &lane_args(json!({}))).map(|_| ()));
    assert!(
        error.contains("demo:HELD is held by live lane run-held"),
        "{error}"
    );
    let error =
        refusal(crate::placement::native_spawn(state.path(), &lane_args(json!({}))).map(|_| ()));
    assert!(error.contains("run-held"), "{error}");
}

#[test]
fn native_dispatch_ignores_review_runs_and_free_keys() {
    let reviewing = lane_state("review");
    assert!(crate::placement::admit_lane(reviewing.path(), &lane_args(json!({}))).is_ok());
    let empty = tempfile::tempdir().unwrap();
    let admitted = crate::placement::admit_lane(empty.path(), &lane_args(json!({})))
        .ok()
        .unwrap();
    assert_eq!(admitted["card"]["brief_items"], json!(["demo:HELD"]));
    assert!(admitted["card"].get("lane_override").is_none());
}

#[test]
fn native_dispatch_override_needs_a_reason_and_is_kept_on_the_card() {
    let state = lane_state("implement");
    for blank in [json!(""), json!("  ")] {
        let args = lane_args(json!({"lane_override_reason": blank}));
        assert!(crate::placement::admit_lane(state.path(), &args).is_err());
    }
    let args = lane_args(
        json!({"lane_override_reason":" second lane on purpose ", "lane_override":"forged"}),
    );
    let admitted = crate::placement::admit_lane(state.path(), &args)
        .ok()
        .unwrap();
    assert_eq!(admitted["card"]["lane_override"], "second lane on purpose");
    // A forged override on a free key is dropped, never recorded.
    let free = tempfile::tempdir().unwrap();
    let forged = lane_args(json!({"lane_override":"forged"}));
    let admitted = crate::placement::admit_lane(free.path(), &forged)
        .ok()
        .unwrap();
    assert!(admitted["card"].get("lane_override").is_none());
}
