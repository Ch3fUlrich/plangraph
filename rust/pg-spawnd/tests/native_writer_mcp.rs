//! Native runs delegated to Python keep the same owner for result and cancel.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
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
    let prompt = format!("Brief\n### {key} | t\nWhy: x\n");
    fixture.client.request(
        "tools/call",
        json!({"name":"plangraph_agent_spawn","arguments":{
            "card":{"run_kind":"native-codex","repo":"/repo"},"prompt":prompt}}),
    )
}

fn error_text(reply: &Value) -> String {
    reply["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .to_owned()
}

fn claim_files(state: &std::path::Path) -> usize {
    pg_spawnd::lane_reservation::claim_files(state)
}

#[test]
fn native_mcp_spawn_failing_in_the_adapter_releases_its_reservation_for_the_retry() {
    let mut fixture = Fixture::new(false, "running");
    let state = fixture.state.path().to_owned();
    // The adapter records the claim it can see while it runs, then refuses.
    let script = format!(
        "#!/bin/sh\nif [ \"$3\" = spawn ]; then\n  ls '{0}'/lane-reservations/*/*.json >> '{0}/seen' 2>/dev/null\n  echo call >> '{0}/spawns'\n  echo 'fixture refused' >&2\n  exit 2\nfi\n",
        state.display()
    );
    fs::write(state.join("adapter"), script).unwrap();
    for _ in 0..2 {
        let reply = spawn_key(&mut fixture, "demo:RETRY");
        assert!(error_text(&reply).contains("fixture refused"), "{reply}");
        assert_eq!(claim_files(&state), 0);
    }
    // Both attempts reached the adapter with the key reserved; the first did not block the retry.
    assert_eq!(
        fs::read_to_string(state.join("spawns"))
            .unwrap()
            .lines()
            .count(),
        2
    );
    assert_eq!(
        fs::read_to_string(state.join("seen"))
            .unwrap()
            .lines()
            .count(),
        2
    );
}

#[test]
fn native_mcp_spawn_registers_a_key_with_a_second_colon_and_the_run_then_holds_it() {
    let mut fixture = Fixture::new(false, "running");
    let state = fixture.state.path().to_owned();
    let script = format!(
        "#!/bin/sh\nif [ \"$3\" = spawn ]; then\n  echo call >> '{0}/spawns'\n  mkdir -p '{0}/runs/run-new'\n  printf '%s' '{{\"run_id\":\"run-new\",\"status\":\"running\",\"role\":\"implement\",\"item_key\":\"demo:ITEM:detail\"}}' > '{0}/runs/run-new/record.json'\n  printf '%s' '{{\"run_id\":\"run-new\",\"run_kind\":\"native-codex\",\"status\":\"running\"}}'\nfi\n",
        state.display()
    );
    fs::write(state.join("adapter"), script).unwrap();
    assert!(spawn_key(&mut fixture, "demo:ITEM:detail")["error"].is_null());
    assert_eq!(claim_files(&state), 0);
    // The registered record holds the full key, not its prefix before the second colon.
    let error = error_text(&spawn_key(&mut fixture, "demo:ITEM:detail"));
    assert!(
        error.contains("demo:ITEM:detail is held by live lane run-new"),
        "{error}"
    );
    assert!(spawn_key(&mut fixture, "demo:ITEM")["error"].is_null());
    assert_eq!(
        fs::read_to_string(state.join("spawns"))
            .unwrap()
            .lines()
            .count(),
        2
    );
}
