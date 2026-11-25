//! The lane guard at the launch boundary: the `lane-admit` verb, the Python `native_spawn` that
//! calls it on the host that runs the writer, and the record a launch publishes from its card.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
#[path = "../../test_support/python.rs"]
mod test_python;
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};

fn write_run(state: &Path, id: &str, record: &Value) {
    fs::create_dir_all(state.join("runs").join(id)).unwrap();
    fs::write(
        state.join("runs").join(id).join("record.json"),
        record.to_string(),
    )
    .unwrap();
}

/// The verb as the Python launch boundary calls it: JSON on stdin, the card to register on stdout.
fn lane_admit(state: &Path, prompt: &str, card: Value) -> Result<Value, String> {
    let mut child = Command::new(binary::spawnd())
        .args(["lane-admit", "--state"])
        .arg(state)
        .env_remove("PLANGRAPH_SPAWN_ITEM")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(json!({"prompt":prompt,"card":card}).to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    if output.status.success() {
        Ok(serde_json::from_slice(&output.stdout).unwrap())
    } else {
        assert_eq!(output.status.code(), Some(2));
        Err(String::from_utf8(output.stderr).unwrap().trim().to_owned())
    }
}

/// What the spawner and the supervisor publish for an admitted launch: the record the production
/// `annotate_item` writes from the card, then the mark and the saved brief the supervisor adds.
fn register(state: &Path, id: &str, prompt: &str, card: &Value) {
    let mut python = test_python::command()
        .args([
            "-c",
            "import json, sys\nfrom plangraph.spawner.item_key import annotate_item\n\
             record = dict(run_id=sys.argv[1], status='running', role='implement')\n\
             annotate_item(record, json.load(sys.stdin))\nprint(json.dumps(record))",
            id,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    python
        .stdin
        .take()
        .unwrap()
        .write_all(card.to_string().as_bytes())
        .unwrap();
    let output = python.wait_with_output().unwrap();
    assert!(output.status.success());
    let mut record: Value = serde_json::from_slice(&output.stdout).unwrap();
    pg_spawnd::lane_guard::publish_marker(&json!({"card":card}), &mut record);
    let brief = pg_spawnd::batch_writer_brief::render(prompt).unwrap();
    record["manifest"] = json!({"brief": brief});
    write_run(state, id, &record);
}

fn launch(state: &Path, id: &str, prompt: &str, card: Value) -> Result<(), String> {
    let admitted = lane_admit(state, prompt, card)?;
    register(state, id, prompt, &admitted);
    Ok(())
}

fn typed(items: Value) -> String {
    let mut input: Value = serde_json::from_str(
        include_str!("fixtures/batch-writer-input.txt")
            .replace("\r\n", "\n")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap(),
    )
    .unwrap();
    input["items"] = items;
    format!("PLANGRAPH-BATCH-INPUT-V1\n{input}")
}

#[test]
fn the_verb_admits_free_keys_into_the_card_and_refuses_held_ones_naming_the_run() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-held",
        &json!({"status":"running","role":"implement","item_key":"demo:HELD"}),
    );
    let card = json!({"run_kind":"native-codex","repo":"/r","lane_admitted":false});
    let admitted = lane_admit(state.path(), "### demo:FREE | t\nWhy: x\n", card.clone()).unwrap();
    assert_eq!(admitted["item_keys"], json!(["demo:FREE"]));
    assert_eq!(admitted["lane_admitted"], true);
    let error = lane_admit(
        state.path(),
        "### demo:FREE | t\n",
        json!({"item":"demo:HELD"}),
    )
    .unwrap_err();
    assert!(
        error.starts_with("lane_collision: demo:HELD is held by live lane run-held"),
        "{error}"
    );
    let members: Vec<String> = (0..51).map(|n| format!("demo:K{n}")).collect();
    let error = lane_admit(state.path(), "x", json!({"item_keys":members})).unwrap_err();
    assert!(error.starts_with("lane_too_many_keys:"), "{error}");
    write_run(state.path(), "run-odd", &json!({"role":"implement"}));
    let error = lane_admit(state.path(), "### demo:FREE | t\n", card).unwrap_err();
    assert!(
        error.starts_with("lane_unreadable:") && error.contains("run-odd"),
        "{error}"
    );
}

#[test]
fn a_registered_run_holds_every_admitted_member_and_a_trailing_colon_key() {
    let state = tempfile::tempdir().unwrap();
    let prompt = "Brief\n### demo:ITEM:detail | t\nWhy: x\n### demo:SECOND | t\nWhy: y\n";
    launch(
        state.path(),
        "run-a",
        prompt,
        json!({"item_key":"demo:CARD:"}),
    )
    .unwrap();
    for key in ["demo:ITEM:detail", "demo:SECOND", "demo:CARD:"] {
        let error = launch(
            state.path(),
            "run-b",
            &format!("### {key} | t\n"),
            json!({}),
        )
        .unwrap_err();
        assert!(
            error.contains(&format!("{key} is held by live lane run-a")),
            "{error}"
        );
    }
    launch(
        state.path(),
        "run-c",
        "### demo:ITEM | t\n",
        json!({"item_key":"demo:CARD"}),
    )
    .unwrap();
}

#[test]
fn native_mcp_typed_batch_body_does_not_claim_an_unassigned_key() {
    // A launch through the verb, the production record writer, the supervisor's mark and its
    // saved (rendered) brief: the body heading is in the saved brief and still holds nothing.
    let state = tempfile::tempdir().unwrap();
    let prompt = typed(json!([{"key":"demo:MEMBER","title":"t",
        "brief":"Why: x\n### demo:BODYONLY | a heading inside the item body\n"}]));
    launch(
        state.path(),
        "run-a",
        &prompt,
        json!({"lane_admitted":true}),
    )
    .unwrap();
    let saved: Value = serde_json::from_str(
        &fs::read_to_string(state.path().join("runs/run-a/record.json")).unwrap(),
    )
    .unwrap();
    assert!(saved["manifest"]["brief"]
        .as_str()
        .unwrap()
        .contains("### demo:BODYONLY"));
    assert_eq!(saved["item_keys"], json!(["demo:MEMBER"]));
    assert_eq!(saved["lane_admitted"], true);
    launch(state.path(), "run-b", "### demo:BODYONLY | t\n", json!({})).unwrap();
    let error = launch(state.path(), "run-c", "### demo:MEMBER | t\n", json!({})).unwrap_err();
    assert!(
        error.contains("demo:MEMBER is held by live lane run-a"),
        "{error}"
    );
}

#[test]
fn a_public_card_mark_cannot_hide_the_keys_of_the_launch() {
    let state = tempfile::tempdir().unwrap();
    let card = json!({"lane_admitted":true,"item_keys":[]});
    launch(state.path(), "run-a", "### demo:ONE | t\n", card).unwrap();
    let error = launch(state.path(), "run-b", "### demo:ONE | t\n", json!({})).unwrap_err();
    assert!(error.contains("held by live lane run-a"), "{error}");
}

/// Drive `overflow.native_spawn`, the launch boundary, against the real binary. It stops right
/// after the admission: prints the refusal text, or `ADMITTED` and the keys put into the card.
fn native_spawn(state: &Path, prompt: &str, card: &Value) -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("NATIVE_BINARY", binary::spawnd())
        .env("STATE", state)
        .env("PROMPT", prompt)
        .env("CARD", card.to_string())
        .args(["-c", r#"
import asyncio, json, os
from pathlib import Path
from unittest.mock import patch
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner import overflow
from plangraph.spawner.service import SpawnService
service = SpawnService(state=Path(os.environ['STATE']))
card, options = json.loads(os.environ['CARD']), dict(prompt=os.environ['PROMPT'])
real = overflow.lane_admit
class Admitted(Exception):
    pass
async def stop_after_admission(*args):
    raise Admitted(json.dumps(await real(*args)))
try:
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), \
         patch.object(overflow, 'native_repo', return_value=Path('/')), \
         patch.object(overflow, 'lane_admit', stop_after_admission):
        asyncio.run(overflow.native_spawn(service, card, options))
except Admitted as exc:
    print('ADMITTED', exc)
except SpawnRefused as exc:
    print(exc)
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

#[test]
fn native_spawn_refuses_card_only_held_key_naming_the_lane_before_any_run() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-held",
        &json!({"status":"running","role":"implement","item_key":"demo:HELD"}),
    );
    // The brief names no held key: only the card's batch members carry demo:HELD.
    let card =
        json!({"run_kind":"native-codex","repo":"/r","item_keys":["demo:OTHER","demo:HELD"]});
    let text = native_spawn(state.path(), "### demo:OTHER | free\nWhy: x\n", &card);
    assert!(
        text.contains("demo:HELD is held by live lane run-held"),
        "{text}"
    );
    let mut runs: Vec<_> = fs::read_dir(state.path().join("runs"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    runs.sort();
    assert_eq!(runs, ["run-held"]);
}

#[test]
fn native_spawn_admits_on_its_own_hosts_state_only() {
    // A holder in some other state (the MCP host's, when placement sends the launch elsewhere)
    // is not read: admission uses the state of the service that launches.
    let mcp_host = tempfile::tempdir().unwrap();
    write_run(
        mcp_host.path(),
        "run-held",
        &json!({"status":"running","role":"implement","item_key":"demo:HELD"}),
    );
    let target = tempfile::tempdir().unwrap();
    let card = json!({"run_kind":"native-codex","repo":"/r","item_key":"demo:HELD"});
    let text = native_spawn(target.path(), "### demo:HELD | t\n", &card);
    let card: Value = serde_json::from_str(text.strip_prefix("ADMITTED ").expect(&text)).unwrap();
    assert_eq!(card["item_keys"], json!(["demo:HELD"]));
    assert_eq!(card["lane_admitted"], true);
}
