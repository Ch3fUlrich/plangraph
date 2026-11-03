#![cfg(unix)]
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;
struct Fixture {
    state: TempDir,
    _guard: ChildGuard,
    client: McpClient,
}
impl Fixture {
    fn new(output: &str, code: i32, stderr: &str) -> Self {
        let state = TempDir::new();
        let script = state.path().join("adapter");
        std::fs::write(&script, format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'/argv\nprintf '%s' '{}'\nprintf '%s\\n' '{}' >&2\nexit {}\n",state.path().display(),output,stderr,code)).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        let socket = state.path().join("socket");
        let child = std::process::Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("serve")
            .arg("--socket")
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("PG_SPAWND_ADAPTER", script)
            .spawn()
            .unwrap();
        let guard = ChildGuard { child };
        let mut client = McpClient::connect(&socket);
        client.initialize();
        Self {
            state,
            _guard: guard,
            client,
        }
    }
    fn call(&mut self, verb: &str, args: Value) -> Value {
        self.client.request(
            "tools/call",
            json!({"name":format!("plangraph_agent_{verb}"),"arguments":args}),
        )
    }
}
#[test]
fn printed_object_is_unchanged() {
    let printed = r#"{"b":1,"a":2,"f":1e-07}"#;
    for (verb, code) in [("route", 0), ("probe", 0), ("probe", 1)] {
        let mut f = Fixture::new(&format!(" \n{printed}\n "), code, "");
        let r = f.call(verb, json!({"card":{}}));
        assert_eq!(r["result"]["content"][0]["text"], printed);
    }
}

#[test]
fn typed_options_precede_card_size() {
    let mut f = Fixture::new("{}", 0, "");
    for (key, value, message) in [
        (
            "role",
            json!("bad"),
            "role must be implement, review or orchestrate",
        ),
        (
            "writer_families",
            json!(1),
            "writer_families must be an array of strings",
        ),
        ("review_of", json!(1), "review_of must be a string"),
        ("require_bench", json!(1), "require_bench must be a bool"),
        ("bench_db", json!(1), "bench_db must be a string"),
    ] {
        let mut args = json!({"card":"x".repeat(24001)});
        args[key] = value;
        assert_eq!(f.call("route", args)["error"]["message"], message);
    }
}

#[test]
fn argv_and_passthrough() {
    let mut f = Fixture::new(r#"{"ok":false,"plan":1}"#, 0, "skip leg: reason");
    let r=f.call("route",json!({"card":{"x":1},"role":"review","writer_families":["a","b"],"review_of":"run-a","require_bench":true,"bench_db":"db"}));
    assert_eq!(
        serde_json::from_str::<Value>(r["result"]["content"][0]["text"].as_str().unwrap()).unwrap(),
        json!({"ok":false,"plan":1})
    );
    assert_eq!(std::fs::read_to_string(f.state.path().join("argv")).unwrap(),format!("--state\n{}\nroute\n--card\n{{\"x\":1}}\n--role\nreview\n--writer-family\na\n--writer-family\nb\n--review-of\nrun-a\n--require-bench\n--bench-db\ndb\n--json\n",f.state.path().display()));
    f.call(
        "probe",
        json!({"leg":["a","b"],"all_writers":true,"timeout":2}),
    );
    assert_eq!(
        std::fs::read_to_string(f.state.path().join("argv")).unwrap(),
        format!(
            "--state\n{}\nprobe\n--leg\na\n--leg\nb\n--all-writers\n--timeout\n2\n--json\n",
            f.state.path().display()
        )
    );
}
#[test]
fn exit_mapping() {
    for (verb, out, exit, code, kind) in [
        ("route", "{}", 2, -32000, "SpawnRefused"),
        ("route", "{}", 3, -32602, "BenchmarkError"),
        ("route", "{}", 1, -32602, "ValueError"),
        ("route", "{}", 7, -32603, "RuntimeError"),
        ("route", "bad", 0, -32603, "RuntimeError"),
        ("route", "[]", 0, -32603, "RuntimeError"),
        ("probe", "bad", 1, -32602, "ParameterRefused"),
    ] {
        let mut f = Fixture::new(out, exit, "reason");
        let r = f.call(verb, json!({"card":{}}));
        assert_eq!(r["error"]["code"], code, "{r}");
        assert_eq!(r["error"]["data"]["type"], kind, "{r}");
        if exit == 2 || exit == 3 || exit == 1 {
            assert_eq!(r["error"]["message"], "reason");
        }
        if exit == 3 {
            assert_eq!(r["error"]["data"]["exit_code"], 3);
        }
    }
    let mut f = Fixture::new(r#"{"ok":false}"#, 1, "");
    assert!(f.call("probe", json!({}))["result"].is_object());
}
#[test]
fn validation_and_spawn_stub() {
    let mut f = Fixture::new("{}", 0, "");
    for (verb, args, message) in [
        ("route", json!({}), "'card'"),
        (
            "route",
            json!({"card":{},"role":"bad"}),
            "role must be implement, review or orchestrate",
        ),
        (
            "route",
            json!({"card":{},"writer_families":1}),
            "writer_families must be an array of strings",
        ),
        (
            "route",
            json!({"card":{},"require_bench":1}),
            "require_bench must be a bool",
        ),
        (
            "route",
            json!({"card":{},"bench_db":1}),
            "bench_db must be a string",
        ),
        (
            "route",
            json!({"card":"x".repeat(24001)}),
            "card too large for the route adapter",
        ),
        (
            "probe",
            json!({"timeout":-1}),
            "timeout must be a number > 0",
        ),
    ] {
        let r = f.call(verb, args);
        assert_eq!(r["error"]["code"], -32602);
        assert_eq!(r["error"]["message"], message);
    }
    let r = f.call("spawn", json!({}));
    assert_eq!(r["result"]["isError"], true);
    assert_eq!(
        r["result"]["content"][0]["text"],
        "not implemented in pg-spawnd yet (R6a-3b)"
    );
}
