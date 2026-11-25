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
    let error = refusal(crate::lane_admit::admit_lane(
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
}

#[test]
fn native_dispatch_ignores_review_runs_and_free_keys_and_hands_the_keys_to_the_card() {
    let reviewing = lane_state("review");
    assert!(crate::lane_admit::admit_lane(reviewing.path(), &lane_args(json!({}))).is_ok());
    let empty = tempfile::tempdir().unwrap();
    let forged = lane_args(json!({"lane_admitted":false,"item_keys":["demo:ONE","bad key"]}));
    let admitted = admit(crate::lane_admit::admit_lane(empty.path(), &forged));
    assert_eq!(
        admitted["card"]["item_keys"],
        json!(["demo:HELD", "demo:ONE"])
    );
    assert_eq!(admitted["card"]["lane_admitted"], true);
    // A public card cannot hide keys: a mark with an empty list is recomputed from the brief.
    let hiding = lane_args(json!({"lane_admitted":true,"item_keys":[]}));
    let admitted = admit(crate::lane_admit::admit_lane(empty.path(), &hiding));
    assert_eq!(admitted["card"]["item_keys"], json!(["demo:HELD"]));
    // The caller's own card is untouched, and nothing was written to the state.
    assert_eq!(forged["card"]["lane_admitted"], false);
    assert!(files(empty.path()).is_empty());
}

#[test]
fn a_lane_refusal_message_names_its_reason_and_other_messages_name_none() {
    use crate::lane_admit::refusal_reason;
    for reason in ["lane_unreadable", "lane_collision", "lane_too_many_keys"] {
        assert_eq!(refusal_reason(&format!("{reason}: x")), Some(reason));
    }
    for message in [
        "lane_collisions: x",
        "spawn refused",
        "a lane_collision: x",
        "",
    ] {
        assert_eq!(refusal_reason(message), None);
    }
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
    let error = refusal(crate::lane_admit::admit_lane(
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
fn the_key_a_spawner_inherits_from_its_environment_is_admitted_like_a_card_key() {
    let card = json!({"item_keys":["demo:B", "bad"]});
    let inherited = Some("demo:demo:ENV".to_owned());
    assert_eq!(
        crate::lane_admit::card_keys(&card, inherited.clone()),
        ["demo:demo:ENV", "demo:B"]
    );
    // A key the card names wins over the inherited one, as in the run record.
    let card = json!({"item":"demo:CARD","item_key":""});
    assert_eq!(
        crate::lane_admit::card_keys(&card, inherited),
        ["demo:CARD"]
    );
}

#[test]
fn more_keys_than_a_run_record_keeps_refuse_instead_of_being_dropped_by_the_adapter() {
    let state = tempfile::tempdir().unwrap();
    let members: Vec<String> = (0..51).map(|n| format!("demo:K{n}")).collect();
    let error = refusal(crate::lane_admit::admit_lane(
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
        admit(crate::lane_admit::admit_lane(state.path(), &args))["card"]["item_keys"]
            .as_array()
            .unwrap()
            .len(),
        50
    );
}
