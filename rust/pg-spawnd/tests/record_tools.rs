#![cfg(unix)]
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::ffi::OsStr;

fn write(state: &TempDir, id: &str, value: Value) {
    let dir = state.path().join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("record.json"), serde_json::to_vec(&value).unwrap()).unwrap();
}
fn start(state: &TempDir) -> (ChildGuard, McpClient) {
    let socket = state.path().join("socket");
    let guard = ChildGuard::spawn(
        env!("CARGO_BIN_EXE_pg-spawnd"),
        &[
            OsStr::new("serve"),
            OsStr::new("--socket"),
            socket.as_os_str(),
            OsStr::new("--state"),
            state.path().as_os_str(),
        ],
    );
    let mut client = McpClient::connect(&socket);
    client.initialize();
    (guard, client)
}
fn call(client: &mut McpClient, tool: &str, args: Value) -> Value {
    client.request(
        "tools/call",
        json!({"name":format!("plangraph_{tool}"),"arguments":args}),
    )
}
fn success(response: Value, expected: Value) {
    assert_eq!(response["result"]["isError"], false, "{response}");
    assert_eq!(response["result"]["content"].as_array().unwrap().len(), 1);
    assert_eq!(
        response["result"]["content"][0]["text"],
        serde_json::to_string_pretty(&expected).unwrap()
    );
}
fn error(response: Value, message: &str, kind: &str) {
    assert!(response.get("result").is_none(), "{response}");
    assert_eq!(response["error"]["code"], -32602);
    assert_eq!(response["error"]["message"], message);
    assert_eq!(
        response["error"]["data"],
        json!({"type":kind,"exit_code":1})
    );
}

#[test]
fn python_record_verbatim_and_read_errors() {
    let state = TempDir::new();
    // Python-written record shape, deliberately including fields Rust does not own.
    let record = json!({"run_id":"run-a","started":12.5,"status":"completed",
        "pid_start":"fixture-start","watchdog":{"seen":true},
        "completion":{"parent_session":"parent","item":"item"},
        "parent_session":"keep","item_key":"keep-item","unknown":[1,2]});
    write(&state, "run-a", record.clone());
    write(&state, "run-b", json!({"run_id":"run-b","started":1}));
    let (_guard, mut client) = start(&state);
    success(
        call(&mut client, "agent_result", json!({"run_id":"run-a"})),
        record.clone(),
    );
    success(
        call(
            &mut client,
            "agent_status",
            json!({"run_id":"run-a","job_id":"ignored"}),
        ),
        record.clone(),
    );
    success(
        call(&mut client, "agent_ps", json!({})),
        json!({"runs":[record,{"run_id":"run-b","started":1}]}),
    );
    error(
        call(&mut client, "agent_result", json!({})),
        "'run_id'",
        "KeyError",
    );
    for id in [
        json!("../bad"),
        json!("run-missing"),
        json!(null),
        json!(12),
    ] {
        error(
            call(&mut client, "agent_result", json!({"run_id":id})),
            "unknown run id",
            "ValueError",
        );
    }
    error(
        call(&mut client, "agent_status", json!({"run_id":null})),
        "unknown run id",
        "ValueError",
    );
    std::fs::write(state.path().join("runs/run-a/record.json"), "{").unwrap();
    error(
        call(&mut client, "agent_result", json!({"run_id":"run-a"})),
        "invalid run record",
        "ValueError",
    );
    std::fs::remove_file(state.path().join("runs/run-a/record.json")).unwrap();
    std::fs::create_dir(state.path().join("runs/run-a/record.json")).unwrap();
    error(
        call(&mut client, "agent_result", json!({"run_id":"run-a"})),
        "invalid run record",
        "ValueError",
    );
}

#[test]
fn listing_order_skips_lifts_and_marker() {
    let state = TempDir::new();
    for (id, value) in [
        ("run-a", json!({"run_id":"a"})),
        ("run-b", json!({"run_id":"b","started":"bad"})),
        (
            "run-c",
            json!({"run_id":"c","started":9,"completion":{"parent_session":"p","item":"plangraph:D-5047"}}),
        ),
        ("run-d", json!({"run_id":"d","started":9})),
        ("run-e", json!([])),
        ("run-f", json!({})),
        ("other", json!({"run_id":"other","started":100})),
    ] {
        write(&state, id, value);
    }
    write(&state, "run-g", json!({"run_id":"bad"}));
    std::fs::write(state.path().join("runs/run-g/record.json"), "invalid").unwrap();
    std::fs::create_dir_all(state.path().join("runs/run-h/record.json")).unwrap();
    std::fs::write(state.path().join("runs/run-c/parent.delivered"), "").unwrap();
    let (_guard, mut client) = start(&state);
    success(
        call(&mut client, "agent_ps", json!({})),
        json!({"runs":[
            {"run_id":"c","started":9,"completion":{"parent_session":"p","item":"plangraph:D-5047"},"parent_session":"p","item_key":"plangraph:D-5047","_completion_acknowledged":true},
            {"run_id":"d","started":9},{"run_id":"a"},{"run_id":"b","started":"bad"}
        ]}),
    );
    assert!(!state.path().join("watchdog").exists());
}

#[test]
fn status_steer_refusals_and_stub_slices() {
    let state = TempDir::new();
    write(
        &state,
        "run-a",
        json!({"run_id":"run-a","status":"refused","refusal":"ParameterRefused","error":"bad","legs":["leg"]}),
    );
    let (_guard, mut client) = start(&state);
    for tool in ["agent_result", "agent_status"] {
        let response = call(&mut client, tool, json!({"run_id":"run-a"}));
        assert!(response.get("result").is_none());
        assert_eq!(
            response["error"],
            json!({"code":-32602,"message":"bad","data":{"type":"ParameterRefused","exit_code":2,"legs":["leg"],"run_id":"run-a"}})
        );
    }
    success(
        call(&mut client, "agent_status", json!({})),
        json!({"jobs":[]}),
    );
    error(
        call(&mut client, "agent_status", json!({"job_id":"x"})),
        "'Job x not found'",
        "KeyError",
    );
    for id in [json!(12), json!(true), json!(null)] {
        success(
            call(
                &mut client,
                "agent_steer",
                json!({"job_id":id,"message":42}),
            ),
            json!({"ok":false,"steered":false,"error":"unknown job"}),
        );
    }
    error(
        call(&mut client, "agent_steer", json!({})),
        "'job_id'",
        "KeyError",
    );
    error(
        call(&mut client, "agent_steer", json!({"job_id":"x"})),
        "'message'",
        "KeyError",
    );
    for (tool, slice) in [
        ("agent_query_catalog", "R11"),
        ("session_list", "R9"),
        ("session_launch", "R9"),
        ("session_stop", "R9"),
        ("session_relaunch", "R9"),
        ("session_mark_ready", "R9"),
    ] {
        let response = call(&mut client, tool, json!({}));
        assert_eq!(response["result"]["isError"], true, "{tool}: {response}");
        assert_eq!(
            response["result"]["content"][0]["text"],
            format!("not implemented in pg-spawnd yet ({slice})")
        );
    }
}

#[test]
fn fifo_record_is_invalid_without_blocking() {
    let state = TempDir::new();
    let dir = state.path().join("runs/run-fifo");
    std::fs::create_dir_all(&dir).unwrap();
    let path =
        std::ffi::CString::new(dir.join("record.json").as_os_str().as_encoded_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
    let (_guard, mut client) = start(&state);
    error(
        call(&mut client, "agent_result", json!({"run_id":"run-fifo"})),
        "invalid run record",
        "ValueError",
    );
}
