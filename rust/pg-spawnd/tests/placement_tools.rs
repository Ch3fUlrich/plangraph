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
        Self::with_host(output, code, stderr, "local")
    }
    fn with_host(output: &str, code: i32, stderr: &str, host: &str) -> Self {
        let state = TempDir::new();
        let script = state.path().join("adapter");
        std::fs::write(&script, format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'/argv\nprintf '%s' '{}'\nprintf '%s\\n' '{}' >&2\nexit {}\n",state.path().display(),output,stderr,code)).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        let launch = state.path().join("launch");
        std::fs::write(&launch, format!("#!/bin/sh\ncat >/dev/null\nprintf '%s' '{{\"argv\":[\"/bin/true\"],\"cwd\":\"{}\",\"env\":{{}},\"prompt\":\"fixture prompt\",\"stdin\":\"fixture prompt\"}}'\n", std::env::current_dir().unwrap().display())).unwrap();
        std::fs::set_permissions(&launch, std::fs::Permissions::from_mode(0o755)).unwrap();
        let socket = state.path().join("socket");
        let child = std::process::Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("serve")
            .arg("--no-scope")
            .arg("--socket")
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("PLANGRAPH_HOST", host)
            .env("PG_SPAWND_ADAPTER", script)
            .env("PG_SPAWND_LAUNCH_CMD", launch)
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
fn route_probe_validation() {
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
}

fn records(f: &Fixture) -> Vec<Value> {
    std::fs::read_dir(f.state.path().join("runs"))
        .into_iter()
        .flatten()
        .map(|e| {
            serde_json::from_slice(&std::fs::read(e.unwrap().path().join("record.json")).unwrap())
                .unwrap()
        })
        .collect()
}
#[test]
fn spawn_validation_records() {
    let mut cases = vec![
        (json!({}), "'prompt'".to_string(), "KeyError", 1, "failed"),
        (
            json!({"prompt":"p"}),
            "spawn by card; card required".into(),
            "ParameterRefused",
            2,
            "refused",
        ),
    ];
    for (key, value, message) in [
        ("card", json!({}), "spawn by card; card required"),
        ("card", json!(null), "spawn by card; card required"),
        ("prompt", json!(1), "prompt must be a string"),
        ("card", json!([1]), "card must be an object"),
        (
            "role",
            json!("orchestrate"),
            "role must be implement or review",
        ),
        ("dry_run", json!(1), "dry_run must be a bool"),
        (
            "writer_families",
            json!(1),
            "writer_families must be an array of strings",
        ),
        ("require_bench", json!(1), "require_bench must be a bool"),
        ("bench_db", json!(1), "bench_db must be a string"),
        ("review_of", json!(1), "review_of must be a string"),
        (
            "wait_seconds",
            json!(-1),
            "wait_seconds must be a number >= 0",
        ),
    ] {
        let mut a = json!({"prompt":"p","card":{"x":1}});
        a[key] = value;
        cases.push((a, message.into(), "ParameterRefused", 2, "refused"));
    }
    for option in [
        "seats",
        "model_or_combo",
        "override_reason",
        "review_scope",
        "card.findings",
    ] {
        let mut a = json!({"prompt":"p","card":{"x":1},"role":"review"});
        if option == "card.findings" {
            a["card"]["findings"] = json!([1]);
            a["role"] = json!("implement");
        } else {
            a[option] = if option == "seats" {
                json!(true)
            } else {
                json!("value")
            };
        }
        cases.push((
            a,
            format!("{option} is not supported by pg-spawnd yet; use the Python spawner"),
            "ParameterRefused",
            2,
            "refused",
        ));
    }
    let mut f = Fixture::new("{}", 0, "");
    for (args, message, kind, exit, status) in cases {
        let before: Vec<_> = records(&f).iter().map(|r| r["run_id"].clone()).collect();
        let role = args.get("role").cloned().unwrap_or(json!("implement"));
        let r = f.call("spawn", args);
        assert_eq!(r["error"]["code"], -32602, "{r}");
        assert_eq!(r["error"]["message"], message);
        assert_eq!(r["error"]["data"]["type"], kind);
        let all: Vec<_> = records(&f)
            .into_iter()
            .filter(|r| !before.contains(&r["run_id"]))
            .collect();
        assert_eq!(all.len(), 1);
        let row = &all[0];
        keys(row, "attempts ended error events evidence exit_code host legs refusal role run_id served_family served_model served_witness_reason started status stderr_tail stdout_tail wait_cmd");
        assert_eq!(row["host"], "local");
        assert_eq!(row["error"], message);
        assert_eq!(row["status"], status);
        assert_eq!(row["exit_code"], exit);
        assert_eq!(row["refusal"], kind);
        assert_eq!(row["role"], role);
        assert!(row["ended"].is_number());
        assert_eq!(row["legs"], json!([]));
        assert_eq!(row["evidence"], json!([]));
    }
}
#[test]
fn spawn_dry_run_and_no_launch() {
    let plan = json!({"legs":[],"first_leg":{"id":"leg","harness":"h","model":"m","family":"f"}});
    let mut f = Fixture::new(&plan.to_string(), 0, "");
    let mut args = json!({"prompt":"p","card":{"x":1},"dry_run":true,"writer_families":["writer"],"review_of":"run-a","risk":"high","no_fallthrough":true});
    let r = f.call("spawn", args.clone());
    let text = r["result"]["content"][0]["text"].as_str().unwrap();
    let row: Value = serde_json::from_str(text).unwrap();
    assert_eq!(records(&f), vec![row.clone()]);
    let id = row["run_id"].as_str().unwrap();
    let file = std::fs::read_to_string(f.state.path().join("runs").join(id).join("record.json"));
    // JSON float parsing may normalize the last timestamp digit; compare values.
    assert_eq!(row, serde_json::from_str::<Value>(&file.unwrap()).unwrap());
    keys(&row, "attempts card_hash chosen_leg cwd ended events exit_code host item_key job_id pid plan prompt_sha256 request_side_model role run_id served_family served_model served_witness_reason started status stderr_tail stdout_tail wait_cmd wake_variant witness_kind writer_family");
    keys(&row["attempts"][0], "leg provider_response request_side_model served_family served_model served_witness_reason skip_reason status stderr_tail stdout_tail witness_kind");
    keys(&row["plan"], "first_leg legs");
    for leg in [
        &row["chosen_leg"],
        &row["plan"]["first_leg"],
        &row["attempts"][0]["leg"],
    ] {
        keys(leg, "family harness id model");
    }
    assert_eq!(row["status"], "dry_run");
    assert!(row["ended"].is_number());
    assert_eq!(row["plan"], plan);
    assert_eq!(row["chosen_leg"], plan["first_leg"]);
    assert_eq!(row["role"], "implement");
    assert_eq!(row["exit_code"], 0);
    for key in ["request_side_model", "witness_kind"] {
        assert!(row[key].is_null());
    }
    for key in ["writer_family", "served_family"] {
        assert_eq!(row[key], "f");
    }
    assert_eq!(row["served_model"], "m");
    assert_eq!(
        row["cwd"],
        std::env::current_dir().unwrap().to_string_lossy().as_ref()
    );
    let tail = "[dry-run] Planned command: /bin/true [stdin: 14 bytes]";
    assert_eq!(row["stdout_tail"], tail);
    assert_eq!(
        row["attempts"],
        json!([{"leg":plan["first_leg"],"status":"dry_run","served_model":"m","served_family":"f","served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg","request_side_model":null,"witness_kind":null,"skip_reason":null,"provider_response":null,"stdout_tail":tail,"stderr_tail":""}])
    );
    use sha2::{Digest, Sha256};
    assert_eq!(
        row["card_hash"],
        format!("{:x}", Sha256::digest(b"{\"x\": 1}"))
    );
    let argv = std::fs::read_to_string(f.state.path().join("argv")).unwrap();
    assert!(argv.contains("--writer-family\nwriter\n--review-of\nrun-a\n"));
    args["dry_run"] = json!(false);
    args["wait_seconds"] = json!(2);
    let r = f.call("spawn", args);
    assert!(r.get("error").is_none(), "{r}");
    let row: Value =
        serde_json::from_str(r["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(row["status"], "completed");
    assert_eq!(records(&f).len(), 2);
}
#[test]
fn adapter_failures_never_duplicate_records() {
    for (out, code, message) in [
        ("{}", 2, "reason"),
        ("{}", 3, "reason"),
        ("{}", 0, "route adapter returned no leg"),
    ] {
        let mut f = Fixture::new(out, code, "reason");
        let r = f.call("spawn", json!({"prompt":"p","card":{"x":1},"dry_run":true}));
        assert_eq!(r["error"]["message"], message);
        assert!(records(&f).is_empty());
    }
}

#[test]
fn valid_host_is_saved() {
    let mut f = Fixture::with_host("{}", 0, "", "test-host");
    f.call("spawn", json!({}));
    assert_eq!(records(&f)[0]["host"], "test-host");
}

fn keys(value: &Value, expected: &str) {
    let mut actual: Vec<_> = value
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    actual.sort_unstable();
    assert_eq!(actual, expected.split_whitespace().collect::<Vec<_>>());
}
#[test]
fn findings_python_truthiness() {
    let plan = json!({"legs":[],"first_leg":{"id":"leg","harness":"h","model":"m","family":"f"}});
    let mut f = Fixture::new(&plan.to_string(), 0, "");
    for (findings, role, refused) in [
        (json!(false), "implement", false),
        (json!(0), "implement", false),
        (json!(0.0), "implement", false),
        (json!(""), "implement", false),
        (json!([]), "implement", false),
        (json!({}), "implement", false),
        (json!(null), "implement", false),
        (json!(["x"]), "implement", true),
        (json!(["x"]), "review", false),
    ] {
        let r = f.call(
            "spawn",
            json!({"prompt":"p","card":{"findings":findings},"role":role,"dry_run":true}),
        );
        assert_eq!(r.get("error").is_some(), refused, "{r}");
    }
}
