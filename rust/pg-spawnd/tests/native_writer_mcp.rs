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

fn spawn_key(fixture: &mut Fixture, key: &str) -> Value {
    fixture.client.request(
        "tools/call",
        json!({"name":"plangraph_agent_spawn","arguments":{
            "card":{"run_kind":"native-codex","repo":"/repo"},
            "prompt":format!("Brief\n### {key} | t\nWhy: x\n")}}),
    )
}

fn spawns(state: &std::path::Path) -> usize {
    fs::read_to_string(state.join("spawns"))
        .unwrap_or_default()
        .lines()
        .count()
}

/// An adapter that counts its `spawn` calls and answers with `stderr` and exit code `code`.
fn counting_adapter(state: &std::path::Path, answer: &str) {
    let script = format!(
        "#!/bin/sh\nif [ \"$3\" = spawn ]; then\n  echo call >> '{}/spawns'\n  {answer}\nfi\n",
        state.display()
    );
    pg_worker_core::executable_test_support::write_private_executable(
        state.join("adapter"),
        script,
    );
}

#[test]
fn native_mcp_spawn_leaves_the_lane_check_to_the_host_that_runs_the_writer() {
    // The MCP host holds the key, but placement may send the launch to another host: the check
    // belongs to `overflow.native_spawn` on the host that runs it, so the adapter is reached.
    let mut fixture = Fixture::new(false, "running");
    let state = fixture.state.path().to_owned();
    fs::write(
        state.join("runs/run-native/record.json"),
        json!({"run_id":"run-native","status":"running","role":"implement","item_key":"demo:HELD"})
            .to_string(),
    )
    .unwrap();
    counting_adapter(
        &state,
        r#"printf '%s' '{"run_id":"run-new","run_kind":"native-codex","status":"running"}'"#,
    );
    assert!(spawn_key(&mut fixture, "demo:HELD")["error"].is_null());
    assert_eq!(spawns(&state), 1);
}

#[test]
fn native_mcp_spawn_reports_a_lane_refusal_of_the_launch_boundary_with_its_reason() {
    let mut fixture = Fixture::new(false, "running");
    let state = fixture.state.path().to_owned();
    for reason in ["lane_unreadable", "lane_collision", "lane_too_many_keys"] {
        counting_adapter(
            &state,
            &format!("echo '{reason}: demo:K is held by live lane run-x (run)' >&2; exit 2"),
        );
        let reply = spawn_key(&mut fixture, "demo:K");
        assert!(
            reply["error"]["message"]
                .as_str()
                .unwrap()
                .starts_with(reason),
            "{reply}"
        );
        assert_eq!(reply["error"]["data"]["reason"], reason, "{reply}");
        assert_eq!(reply["error"]["data"]["type"], "ParameterRefused");
    }
    counting_adapter(&state, "echo 'fixture refused' >&2; exit 2");
    let reply = spawn_key(&mut fixture, "demo:K");
    assert!(reply["error"]["data"].get("reason").is_none(), "{reply}");
}

/// The adapter the production spawner presents for a native launch refused at its launch
/// boundary: `overflow.native_spawn` with the real `lane-admit` verb, then the refused record the
/// supervisor prints (`service.save_refusal`), as a JSON reply with a `run_id` and exit code 2.
fn python_launch_boundary_adapter(state: &std::path::Path) {
    fs::write(
        state.join("adapter.py"),
        r#"import asyncio, json, os, sys
from pathlib import Path
from unittest.mock import patch
args = sys.argv[1:]
card = json.loads(args[args.index('--card') + 1])
prompt = args[args.index('--') + 1]
from plangraph.spawner import overflow
from plangraph.spawner.service import SpawnService
service = SpawnService(state=Path(args[args.index('--state') + 1]))
try:
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), \
         patch.object(overflow, 'native_repo', return_value=Path('/')):
        asyncio.run(overflow.native_spawn(service, card, dict(prompt=prompt)))
    sys.exit('the launch was admitted')
except Exception as exc:
    record = service.save_refusal(exc, 'implement', card=card)
    print(json.dumps(record))
    sys.exit(record['exit_code'])
"#,
    )
    .unwrap();
    let python = test_python::command();
    let script = format!(
        "#!/bin/sh\nPYTHONPATH='{}' NATIVE_BINARY='{}' exec '{}' '{}/adapter.py' \"$@\"\n",
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../src")
            .display(),
        binary::spawnd(),
        python.get_program().to_string_lossy(),
        state.display(),
    );
    pg_worker_core::executable_test_support::write_private_executable(
        state.join("adapter"),
        script,
    );
}

#[test]
fn native_mcp_spawn_through_the_python_launch_boundary_reports_the_lane_reason() {
    // The production reply of a refused launch is a record with a run_id, not stderr text.
    let mut fixture = Fixture::new(false, "running");
    let state = fixture.state.path().to_owned();
    python_launch_boundary_adapter(&state);
    // The fixture's own run record has no role: its liveness cannot be decided.
    let reply = spawn_key(&mut fixture, "demo:HELD");
    assert!(
        reply["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("lane_unreadable:"),
        "{reply}"
    );
    assert_eq!(
        reply["error"]["data"]["reason"], "lane_unreadable",
        "{reply}"
    );
    assert!(
        reply["error"]["data"]["run_id"]
            .as_str()
            .unwrap()
            .starts_with("run-"),
        "{reply}"
    );
    fs::write(
        state.join("runs/run-native/record.json"),
        json!({"run_id":"run-native","status":"running","role":"implement","item_key":"demo:HELD"})
            .to_string(),
    )
    .unwrap();
    let reply = spawn_key(&mut fixture, "demo:HELD");
    let message = reply["error"]["message"].as_str().unwrap();
    assert!(
        message.contains("demo:HELD is held by live lane run-native"),
        "{reply}"
    );
    assert_eq!(
        reply["error"]["data"]["reason"], "lane_collision",
        "{reply}"
    );
}

#[test]
fn native_mcp_spawn_wait_reports_settled_refusal() {
    let mut fixture = Fixture::new(false, "running");
    let adapter = fixture.state.path().join("adapter");
    pg_worker_core::executable_test_support::write_private_executable(
        adapter,
        r#"#!/bin/sh
while [ "$#" -gt 1 ]; do
    if [ "$1" = '--' ]; then break; fi
    if [ "$1" = '--wait' ] && [ "$2" = 1.25 ]; then
        printf '%s' '{"run_id":"run-native","run_kind":"native-codex","status":"refused","ended":1760000000,"exit_code":0,"error":"fixture refused"}'
        exit 1
    fi
    shift
done
echo 'native wait was not forwarded' >&2
exit 2
"#,
    );
    let reply = fixture.client.request(
        "tools/call",
        json!({"name":"plangraph_agent_spawn","arguments":{
            "card":{"run_kind":"native-codex","repo":"/repo"},
            "prompt":"brief", "wait_seconds":1.25
        }}),
    );
    assert_eq!(reply["error"]["message"], "fixture refused", "{reply}");
    assert_eq!(reply["error"]["data"]["run_id"], "run-native", "{reply}");
    assert_eq!(reply["error"]["data"]["exit_code"], 1, "{reply}");
}
