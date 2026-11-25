//! Native runs delegated to Python keep the same owner for result and cancel.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
#[path = "../../test_support/python.rs"]
mod test_python;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::{
    fs,
    process::{Command, Stdio},
};

struct Fixture {
    state: TempDir,
    _guard: ChildGuard,
    client: McpClient,
}
impl Fixture {
    fn new(overflow: bool, result_status: &str) -> Self {
        let state = TempDir::new();
        let record = json!({"run_id":"run-native","run_kind":"native-codex","host":"target","status":"running","overflow_remote":overflow});
        let completed = json!({"status":result_status,"final_head":"final-head","exit_code":0});
        let mut response = record.clone();
        for (key, value) in completed.as_object().unwrap() {
            response[key] = value.clone();
        }
        let mut cancelled = record.clone();
        cancelled["status"] = json!("cancelled");
        let adapter = state.path().join("adapter");
        pg_worker_core::executable_test_support::write_private_executable(&adapter, format!("#!/bin/sh\nprintf '%s %s\\n' \"$3\" \"$4\" >> '{}/calls'\nif [ \"$3\" = cancel ]; then printf '%s' '{}'; else printf '%s' '{}'; fi\n", state.path().display(), cancelled, response));
        let socket = state.path().join("socket");
        let child = Command::new(binary::spawnd())
            .args(["serve", "--no-scope", "--socket"])
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("PG_SPAWND_ADAPTER", &adapter)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let guard = ChildGuard { child };
        let mut client = McpClient::connect(&socket);
        client.initialize();
        let run = state.path().join("runs/run-native");
        fs::create_dir_all(&run).unwrap();
        fs::write(run.join("record.json"), record.to_string()).unwrap();
        Self {
            state,
            _guard: guard,
            client,
        }
    }
    fn call(&mut self, verb: &str) -> Value {
        let reply = self.client.request(
            "tools/call",
            json!({"name":format!("plangraph_agent_{verb}"),"arguments":{"run_id":"run-native"}}),
        );
        serde_json::from_str(
            reply["result"]["content"][0]["text"]
                .as_str()
                .unwrap_or_else(|| panic!("{reply}")),
        )
        .unwrap()
    }
}
#[test]
fn native_mcp_result_refreshes_target_receipt() {
    let mut fixture = Fixture::new(true, "completed");
    let result = fixture.call("result");
    assert_eq!(result["status"], "completed");
    assert_eq!(result["final_head"], "final-head");
    assert_eq!(
        fs::read_to_string(fixture.state.path().join("calls")).unwrap(),
        "result run-native\n"
    );
}
#[test]
fn native_mcp_cancel_delegates_to_python_owner_locally_and_remotely() {
    for overflow in [false, true] {
        let mut fixture = Fixture::new(overflow, "running");
        assert_eq!(fixture.call("cancel")["status"], "cancelled");
        let calls = fs::read_to_string(fixture.state.path().join("calls")).unwrap();
        assert!(calls.ends_with("cancel run-native\n"), "{calls}");
    }
}

#[test]
fn native_mcp_spawn_preserves_dash_prompts_and_reports_adapter_refusals() {
    let mut fixture = Fixture::new(false, "running");
    let adapter = fixture.state.path().join("adapter");
    fs::write(
        &adapter,
        r#"#!/bin/sh
for argument in "$@"; do
    if [ "$argument" = '--' ]; then separator=yes; fi
    if [ "$argument" = '--dry-run' ] && [ "$separator" = yes ]; then
        printf '%s' '{"run_id":"run-native","run_kind":"native-codex","status":"running"}'
        exit 0
    fi
done
echo 'fixture refused' >&2
exit 2
"#,
    )
    .unwrap();
    for (prompt, code) in [("--dry-run", None), ("brief", Some(-32000))] {
        let reply = fixture.client.request("tools/call", json!({"name":"plangraph_agent_spawn","arguments":{"card":{"run_kind":"native-codex","repo":"/repo"},"prompt":prompt}}));
        if let Some(code) = code {
            assert_eq!(reply["error"]["code"], code, "{reply}");
            assert!(
                reply["error"]["message"]
                    .as_str()
                    .unwrap()
                    .contains("fixture refused"),
                "{reply}"
            );
        } else {
            assert!(reply["error"].is_null(), "{reply}");
            assert!(
                reply["result"]["content"][0]["text"]
                    .as_str()
                    .unwrap()
                    .contains("run-native"),
                "{reply}"
            );
        }
    }
}

fn spawn(fixture: &mut Fixture, prompt: &str, card_extra: Value) -> Value {
    let mut card = json!({"run_kind":"native-codex","repo":"/repo"});
    card.as_object_mut()
        .unwrap()
        .extend(card_extra.as_object().unwrap().clone());
    fixture.client.request(
        "tools/call",
        json!({"name":"plangraph_agent_spawn","arguments":{"card":card,"prompt":prompt}}),
    )
}

fn spawn_key(fixture: &mut Fixture, key: &str) -> Value {
    spawn(
        fixture,
        &format!("Brief\n### {key} | t\nWhy: x\n"),
        json!({}),
    )
}

fn error_text(reply: &Value) -> String {
    reply["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .to_owned()
}

fn spawns(state: &std::path::Path) -> usize {
    fs::read_to_string(state.join("spawns"))
        .unwrap_or_default()
        .lines()
        .count()
}

/// A fixture whose state holds no run yet, with an adapter that registers a run the way the
/// production spawner does: its record is written by `annotate_item` from the card it receives,
/// so a card member the spawner drops is not held either. The record also takes the saved brief
/// from `rendered-brief` and the admission mark of the card, as the supervisor publishes them.
fn registering_fixture() -> Fixture {
    let fixture = Fixture::new(false, "running");
    let state = fixture.state.path();
    fs::remove_dir_all(state.join("runs")).unwrap();
    let python = test_python::command();
    let script = format!(
        r#"#!/bin/sh
if [ "$3" = spawn ]; then
  echo call >> '{state}/spawns'
  printf '%s' "$5" > '{state}/card-seen'
  n=$(wc -l < '{state}/spawns' | tr -d ' ')
  PYTHONPATH='{src}' '{python}' - "$5" "{state}" "run-new$n" <<'PY'
import json, pathlib, sys
from plangraph.spawner.item_key import annotate_item
card, state, run_id = json.loads(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
record = dict(run_id=run_id, run_kind='native-codex', status='running', role='implement')
annotate_item(record, card)
brief = state / 'rendered-brief'
if brief.exists():
    record['manifest'] = dict(brief=brief.read_text())
if card.get('lane_keys_admitted') is True:
    record['lane_keys_admitted'] = True
(state / 'runs' / run_id).mkdir(parents=True)
(state / 'runs' / run_id / 'record.json').write_text(json.dumps(record))
print(json.dumps(dict(run_id=run_id, run_kind='native-codex', status='running')))
PY
fi
"#,
        state = state.display(),
        src = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../src")
            .display(),
        python = python.get_program().to_string_lossy(),
    );
    pg_worker_core::executable_test_support::write_private_executable(
        state.join("adapter"),
        script,
    );
    fixture
}

#[test]
fn native_mcp_spawn_hands_the_admitted_keys_to_the_adapter_and_the_registered_run_holds_them() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    let prompt = "Brief\n### demo:ITEM:detail | t\nWhy: x\n### demo:SECOND | t\nWhy: y\n";
    assert!(spawn(&mut fixture, prompt, json!({}))["error"].is_null());
    // The record the adapter built from the received card holds every member, the second too.
    for key in ["demo:ITEM:detail", "demo:SECOND"] {
        let error = error_text(&spawn_key(&mut fixture, key));
        assert!(
            error.contains(&format!("{key} is held by live lane run-new1")),
            "{error}"
        );
    }
    assert!(spawn_key(&mut fixture, "demo:ITEM")["error"].is_null());
    assert_eq!(spawns(&state), 2);
}

#[test]
fn native_mcp_card_key_with_a_trailing_colon_survives_the_card_to_record_handoff() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    let card = || json!({"item_key":"demo:ITEM:"});
    assert!(spawn(&mut fixture, "Brief\n", card())["error"].is_null());
    let error = error_text(&spawn(&mut fixture, "Brief\n", card()));
    assert!(
        error.contains("demo:ITEM: is held by live lane run-new1"),
        "{error}"
    );
    assert_eq!(spawns(&state), 1);
}

#[test]
fn native_mcp_spawn_sends_a_near_limit_card_with_the_derived_keys_to_the_adapter() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    // Within the 24,000-byte limit as sent; the admitted keys take it past the limit.
    let base = json!({"run_kind":"native-codex","repo":"/repo","note":""})
        .to_string()
        .len();
    let note = "x".repeat(24000 - base - 4);
    let reply = spawn(
        &mut fixture,
        "Brief\n### demo:BIG | t\nWhy: x\n",
        json!({"note":note}),
    );
    assert!(reply["error"].is_null(), "{reply}");
    let seen: Value =
        serde_json::from_str(&fs::read_to_string(state.join("card-seen")).unwrap()).unwrap();
    assert_eq!(seen["item_keys"], json!(["demo:BIG"]));
    assert!(seen.to_string().len() > 24000);
}

#[test]
fn native_mcp_spawn_refuses_more_keys_than_the_record_persists_before_the_adapter_runs() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    let members: Vec<String> = (0..51).map(|n| format!("demo:K{n}")).collect();
    let reply = spawn(&mut fixture, "Brief\n", json!({"item_keys":members}));
    assert!(
        error_text(&reply).starts_with("lane_too_many_keys:"),
        "{reply}"
    );
    assert_eq!(reply["error"]["data"]["reason"], "lane_too_many_keys");
    assert_eq!(spawns(&state), 0);
    // Fifty members persist whole: the production writer keeps all of them.
    let reply = spawn(&mut fixture, "Brief\n", json!({"item_keys":&members[..50]}));
    assert!(reply["error"].is_null(), "{reply}");
    let record: Value =
        serde_json::from_str(&fs::read_to_string(state.join("runs/run-new1/record.json")).unwrap())
            .unwrap();
    assert_eq!(record["item_keys"].as_array().unwrap().len(), 50);
}

#[test]
fn native_mcp_typed_batch_body_does_not_claim_an_unassigned_key() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    let mut input: Value = serde_json::from_str(
        include_str!("fixtures/batch-writer-input.txt")
            .replace("\r\n", "\n")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap(),
    )
    .unwrap();
    input["items"] = json!([{"key":"demo:MEMBER","title":"t",
        "brief":"Why: x\n### demo:BODYONLY | a heading inside the item body\n"}]);
    let prompt = format!("PLANGRAPH-BATCH-INPUT-V1\n{input}");
    // What the supervisor saves after the launch: the rendered brief, headings of the body included.
    fs::write(
        state.join("rendered-brief"),
        pg_spawnd::batch_writer_brief::render(&prompt)
            .unwrap()
            .as_ref(),
    )
    .unwrap();
    assert!(spawn(&mut fixture, &prompt, json!({}))["error"].is_null());
    assert!(fs::read_to_string(state.join("rendered-brief"))
        .unwrap()
        .contains("### demo:BODYONLY"));
    assert!(spawn_key(&mut fixture, "demo:BODYONLY")["error"].is_null());
    let error = error_text(&spawn_key(&mut fixture, "demo:MEMBER"));
    assert!(
        error.contains("demo:MEMBER is held by live lane run-new1"),
        "{error}"
    );
}

#[test]
fn native_mcp_spawn_refuses_while_a_run_record_cannot_be_judged() {
    let mut fixture = registering_fixture();
    let state = fixture.state.path().to_owned();
    fs::create_dir_all(state.join("runs/run-odd")).unwrap();
    fs::write(
        state.join("runs/run-odd/record.json"),
        r#"{"role":"implement"}"#,
    )
    .unwrap();
    let reply = spawn_key(&mut fixture, "demo:ANY");
    assert!(
        error_text(&reply).starts_with("lane_unreadable:"),
        "{reply}"
    );
    assert_eq!(reply["error"]["data"]["reason"], "lane_unreadable");
    assert_eq!(spawns(&state), 0);
}
