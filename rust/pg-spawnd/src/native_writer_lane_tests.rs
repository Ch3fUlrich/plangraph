use crate::errors::ToolError;
use serde_json::{json, Value};
use std::path::Path;

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

fn refusal<T>(result: Result<T, ToolError>) -> String {
    match result {
        Ok(_) => panic!("launch was admitted"),
        Err(error) => error.into_error_data().message.to_string(),
    }
}

fn admit<T>(result: Result<T, ToolError>) -> T {
    match result {
        Ok(admitted) => admitted,
        Err(error) => panic!("launch refused: {}", error.into_error_data().message),
    }
}

fn reservations(state: &Path) -> usize {
    std::fs::read_dir(state.join("lane-reservations"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        .count()
}

fn run_dirs(state: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(state.join("runs"))
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn native_dispatch_refuses_a_key_held_by_a_live_writer_lane_and_names_it() {
    let state = lane_state("implement");
    let error = refusal(crate::placement::admit_lane(
        state.path(),
        &lane_args(json!({})),
    ));
    assert!(
        error.contains("demo:HELD is held by live lane run-held"),
        "{error}"
    );
    let error = refusal(crate::placement::native_spawn(
        state.path(),
        &lane_args(json!({})),
    ));
    assert!(error.contains("run-held"), "{error}");
}

#[test]
fn native_dispatch_ignores_review_runs_and_free_keys() {
    let reviewing = lane_state("review");
    assert!(crate::placement::admit_lane(reviewing.path(), &lane_args(json!({}))).is_ok());
    let empty = tempfile::tempdir().unwrap();
    let (admitted, _launched) = admit(crate::placement::admit_lane(
        empty.path(),
        &lane_args(json!({})),
    ));
    assert_eq!(admitted["card"]["item_keys"], json!(["demo:HELD"]));
    assert!(admitted["card"].get("lane_override").is_none());
}

#[test]
fn override_with_reason_is_recorded_and_blank_is_refused() {
    let state = lane_state("implement");
    for blank in [json!(""), json!("  ")] {
        let args = lane_args(json!({"lane_override_reason": blank}));
        let error = refusal(crate::placement::admit_lane(state.path(), &args));
        assert!(error.contains("nonblank reason"), "{error}");
    }
    let args = lane_args(json!({"lane_override_reason": 3}));
    assert!(crate::placement::admit_lane(state.path(), &args).is_err());
    let args = lane_args(json!({"lane_override_reason":" second lane on purpose "}));
    let (admitted, _launched) = admit(crate::placement::admit_lane(state.path(), &args));
    assert_eq!(admitted["card"]["lane_override"], "second lane on purpose");
    assert_eq!(admitted["card"]["item_keys"], json!(["demo:HELD"]));
}

#[test]
fn card_lane_override_is_stripped() {
    // A caller-supplied override never reaches the run record, held key or free key.
    let state = lane_state("implement");
    let args = lane_args(json!({"lane_override":"forged"}));
    assert!(refusal(crate::placement::admit_lane(state.path(), &args)).contains("run-held"));
    let free = tempfile::tempdir().unwrap();
    let (admitted, _launched) = admit(crate::placement::admit_lane(free.path(), &args));
    assert!(admitted["card"].get("lane_override").is_none());
    // Beside a real reason, the recorded value is the admitted reason, not the forged text.
    let both = lane_args(json!({"lane_override":"forged","lane_override_reason":"real"}));
    let (admitted, _launched) = admit(crate::placement::admit_lane(state.path(), &both));
    assert_eq!(admitted["card"]["lane_override"], "real");
}

#[test]
fn native_spawn_refuses_card_only_held_key_naming_the_lane_before_any_run() {
    let state = lane_state("implement");
    // The brief names no held key: only the card's batch members carry demo:HELD.
    let mut args = lane_args(json!({"item_keys":["demo:OTHER","demo:HELD"]}));
    args["prompt"] = json!("Brief\n### demo:OTHER | free\nWhy: x\n");
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(
        error.contains("demo:HELD is held by live lane run-held"),
        "{error}"
    );
    // No run was created and nothing was reserved for the free member.
    assert_eq!(run_dirs(state.path()), ["run-held"]);
    assert_eq!(reservations(state.path()), 0);
    // The singular card keys are checked the same way.
    for key in ["item_key", "item"] {
        let mut args = lane_args(json!({ key: "demo:HELD" }));
        args["prompt"] = json!("Brief\n### demo:OTHER | free\nWhy: x\n");
        let error = refusal(crate::placement::native_spawn(state.path(), &args));
        assert!(
            error.contains("demo:HELD is held by live lane run-held"),
            "{key}: {error}"
        );
    }
}

#[test]
fn unsupported_dry_run_leaves_no_reservation() {
    let state = tempfile::tempdir().unwrap();
    let mut args = lane_args(json!({}));
    args.insert("dry_run".into(), json!(true));
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(error.contains("dry_run is not supported"), "{error}");
    assert_eq!(reservations(state.path()), 0);
    // The retry without the option is not blocked by the refused one.
    assert!(crate::placement::admit_lane(state.path(), &lane_args(json!({}))).is_ok());
}

#[test]
fn refused_launch_releases_reservation() {
    let state = tempfile::tempdir().unwrap();
    // The launch is admitted and reserved, then fails while routing: the claim is gone.
    let mut args = lane_args(json!({}));
    args.insert("writer_families".into(), json!(5));
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(!error.contains("held by live lane"), "{error}");
    assert_eq!(reservations(state.path()), 0);
    // While an admitted launch lives its keys stay reserved; its end frees them.
    let (_, launched) = admit(crate::placement::admit_lane(
        state.path(),
        &lane_args(json!({})),
    ));
    assert_eq!(reservations(state.path()), 1);
    let error = refusal(crate::placement::admit_lane(
        state.path(),
        &lane_args(json!({})),
    ));
    assert!(error.contains("held by live lane reservation"), "{error}");
    drop(launched);
    assert_eq!(reservations(state.path()), 0);
    assert!(crate::placement::admit_lane(state.path(), &lane_args(json!({}))).is_ok());
}
