use crate::errors::ToolError;
use serde_json::{json, Value};
use std::path::Path;

fn write_run(state: &Path, id: &str, record: &Value) {
    let run = state.join("runs").join(id);
    std::fs::create_dir_all(&run).unwrap();
    std::fs::write(run.join("record.json"), record.to_string()).unwrap();
}

fn lane_state(role: &str) -> tempfile::TempDir {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-held",
        &json!({"run_id":"run-held","status":"running","role":role,"item_key":"demo:HELD"}),
    );
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

fn refusal<T>(result: Result<T, ToolError>) -> rmcp::ErrorData {
    match result {
        Ok(_) => panic!("launch was admitted"),
        Err(error) => error.into_error_data(),
    }
}

fn admit<T>(result: Result<T, ToolError>) -> T {
    match result {
        Ok(admitted) => admitted,
        Err(error) => panic!("launch refused: {}", error.into_error_data().message),
    }
}

fn files(state: &Path) -> Vec<String> {
    fn walk(dir: &Path, out: &mut Vec<String>) {
        for entry in std::fs::read_dir(dir).into_iter().flatten().flatten() {
            out.push(entry.path().display().to_string());
            if entry.path().is_dir() {
                walk(&entry.path(), out);
            }
        }
    }
    let mut names = Vec::new();
    walk(state, &mut names);
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
        error
            .message
            .contains("demo:HELD is held by live lane run-held"),
        "{}",
        error.message
    );
    assert_eq!(error.data.unwrap()["reason"], "lane_collision");
    let error = refusal(crate::placement::native_spawn(
        state.path(),
        &lane_args(json!({})),
    ));
    assert!(error.message.contains("run-held"), "{}", error.message);
}

#[test]
fn native_dispatch_ignores_review_runs_and_free_keys_and_hands_the_keys_to_the_card() {
    let reviewing = lane_state("review");
    assert!(crate::placement::admit_lane(reviewing.path(), &lane_args(json!({}))).is_ok());
    let empty = tempfile::tempdir().unwrap();
    let forged = lane_args(json!({"lane_keys_admitted":false,"item_keys":["demo:ONE","bad key"]}));
    let admitted = admit(crate::placement::admit_lane(empty.path(), &forged));
    assert_eq!(
        admitted["card"]["item_keys"],
        json!(["demo:HELD", "demo:ONE"])
    );
    assert_eq!(admitted["card"]["lane_keys_admitted"], true);
    // The caller's own card is untouched, and nothing was written to the state.
    assert_eq!(forged["card"]["lane_keys_admitted"], false);
    assert!(files(empty.path()).is_empty());
}

#[test]
fn an_unreadable_run_record_refuses_with_its_own_reason_and_names_the_record() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-odd",
        &json!({"role":"implement","status":"running","x":1}),
    );
    write_run(state.path(), "run-nostatus", &json!({"role":"implement"}));
    let error = refusal(crate::placement::native_spawn(
        state.path(),
        &lane_args(json!({})),
    ));
    assert!(
        error.message.starts_with("lane_unreadable:"),
        "{}",
        error.message
    );
    assert!(error.message.contains("run-nostatus"), "{}", error.message);
    let data = error.data.unwrap();
    assert_eq!(
        (data["reason"].as_str(), data["type"].as_str()),
        (Some("lane_unreadable"), Some("ParameterRefused"))
    );
}

#[test]
fn native_spawn_refuses_card_only_held_key_naming_the_lane_before_any_run() {
    let state = lane_state("implement");
    let before = files(state.path());
    // The brief names no held key: only the card's batch members carry demo:HELD.
    let mut args = lane_args(json!({"item_keys":["demo:OTHER","demo:HELD"]}));
    args["prompt"] = json!("Brief\n### demo:OTHER | free\nWhy: x\n");
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(
        error
            .message
            .contains("demo:HELD is held by live lane run-held"),
        "{}",
        error.message
    );
    assert_eq!(files(state.path()), before);
    // The singular card keys are checked the same way.
    for key in ["item_key", "item"] {
        let mut args = lane_args(json!({ key: "demo:HELD" }));
        args["prompt"] = json!("Brief\n### demo:OTHER | free\nWhy: x\n");
        let error = refusal(crate::placement::native_spawn(state.path(), &args));
        assert!(
            error
                .message
                .contains("demo:HELD is held by live lane run-held"),
            "{key}"
        );
    }
    assert_eq!(files(state.path()), before);
}

#[test]
fn the_key_a_spawner_inherits_from_its_environment_is_admitted_like_a_card_key() {
    let card = json!({"item_keys":["demo:B", "bad"]});
    let inherited = Some("demo:demo:ENV".to_owned());
    assert_eq!(
        crate::placement::card_keys(&card, inherited.clone()),
        ["demo:demo:ENV", "demo:B"]
    );
    // A key the card names wins over the inherited one, as in the run record.
    let card = json!({"item":"demo:CARD","item_key":""});
    assert_eq!(crate::placement::card_keys(&card, inherited), ["demo:CARD"]);
}

#[test]
fn unsupported_dry_run_creates_no_claim_and_no_run() {
    // A keyed request over malformed run state: the dry-run error comes first, so admission
    // never read the state, and nothing was created.
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-odd",
        &json!({"role":"implement","status":7}),
    );
    let before = files(state.path());
    let mut args = lane_args(json!({"item_key":"demo:HELD"}));
    args.insert("dry_run".into(), json!(true));
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(
        error.message.contains("dry_run is not supported"),
        "{}",
        error.message
    );
    assert_eq!(files(state.path()), before);
}

#[test]
fn a_refused_launch_option_never_reaches_admission() {
    // The state is malformed and holds the key: an admitted request would be refused as a lane.
    let state = lane_state("implement");
    write_run(state.path(), "run-odd", &json!({"role":"implement"}));
    let before = files(state.path());
    for (card, expected) in [
        (json!({"push_ref":"main"}), "push_ref must name refs/heads/"),
        (
            json!({"review_leg":"bad leg!","review_override_reason":"a reason long enough"}),
            "review_leg_invalid",
        ),
        (json!({"effort":"xhigh"}), "effort_needs_decision"),
    ] {
        let error = refusal(crate::placement::native_spawn(
            state.path(),
            &lane_args(card),
        ));
        assert!(error.message.contains(expected), "{}", error.message);
        assert!(error.data.unwrap().get("reason").is_none());
    }
    let mut args = lane_args(json!({}));
    args.insert("wait_seconds".into(), json!(-1));
    let error = refusal(crate::placement::native_spawn(state.path(), &args));
    assert!(error.message.contains("wait_seconds"), "{}", error.message);
    assert_eq!(files(state.path()), before);
}

#[test]
fn a_valid_card_keeps_its_size_allowance_after_the_admitted_keys_are_added() {
    let state = tempfile::tempdir().unwrap();
    let mut args = lane_args(json!({}));
    let base = args["card"].to_string().len();
    // The caller's card is within the limit by less than the keys admission attaches.
    args["card"]["note"] = json!("x".repeat(24000 - base - 12));
    assert!(args["card"].to_string().len() <= 24000);
    assert!(crate::placement::route_args(&args).is_ok());
    let admitted = admit(crate::placement::admit_lane(state.path(), &args));
    assert!(admitted["card"].to_string().len() > 24000);
    assert!(crate::placement::route_args(&admitted).is_err());
    assert!(crate::placement::route_argv(&admitted).is_ok());
}

#[test]
fn more_keys_than_a_run_record_keeps_refuse_instead_of_being_dropped_by_the_adapter() {
    let state = tempfile::tempdir().unwrap();
    let members: Vec<String> = (0..51).map(|n| format!("demo:K{n}")).collect();
    let error = refusal(crate::placement::admit_lane(
        state.path(),
        &lane_args(json!({"item_keys":members})),
    ));
    assert!(
        error.message.starts_with("lane_too_many_keys:"),
        "{}",
        error.message
    );
    assert_eq!(error.data.unwrap()["reason"], "lane_too_many_keys");
    let fifty = &members[..50];
    let mut args = lane_args(json!({"item_keys":fifty}));
    args["prompt"] = json!("Brief\n");
    assert_eq!(
        admit(crate::placement::admit_lane(state.path(), &args))["card"]["item_keys"]
            .as_array()
            .unwrap()
            .len(),
        50
    );
}
