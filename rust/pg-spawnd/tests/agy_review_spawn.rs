#![cfg(unix)]
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::{os::unix::fs::PermissionsExt, path::Path, process::Command};

fn git(dir: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}
fn executable(path: &Path, text: &str) {
    std::fs::write(path, text).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
}
fn payload(reply: &Value) -> Value {
    serde_json::from_str(reply["result"]["content"][0]["text"].as_str().unwrap()).unwrap()
}
#[test]
fn review_kind_spawns_a_stamped_repository_review_and_records_it() {
    let state = TempDir::new();
    let repo = state.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    git(&repo, &["init", "-q"]);
    git(
        &repo,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
    let head = git(&repo, &["rev-parse", "HEAD"]);
    let leg = json!({"id":"agy-gemini-4-hydrogen-high", "model":"gemini-4-hydrogen-high", "harness":"agy", "family":"gemini"});
    let frame = json!({"event":"result", "result":{"status":"SUCCESS", "response":"No findings.\nVERDICT: PASS\nTEST_GAMING: no\n"}});
    let program = state.path().join("reviewer");
    executable(
        &program,
        &format!("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{frame}'\n"),
    );
    let route = state.path().join("route");
    executable(
        &route,
        &format!(
            "#!/bin/sh\nprintf '%s' '{}'\n",
            json!({"first_leg":leg, "legs":[leg]})
        ),
    );
    let launch = state.path().join("launch");
    let spec = json!({"argv":[program], "cwd":repo, "env":{}, "stdin":"review", "prompt":"review"});
    executable(
        &launch,
        &format!("#!/bin/sh\ncat >/dev/null\nprintf '%s' '{spec}'\n"),
    );
    let socket = state.path().join("socket");
    let child = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["serve", "--no-scope", "--socket"])
        .arg(&socket)
        .arg("--state")
        .arg(state.path())
        .env("PG_SPAWND_ADAPTER", &route)
        .env("PG_SPAWND_LAUNCH_CMD", &launch)
        .env("PLANGRAPH_RELIABILITY_LEG_REF", "on")
        .env("PLANGRAPH_INLINE_REVIEW_DIFF", "off")
        .spawn()
        .unwrap();
    let _guard = ChildGuard { child };
    let mut client = McpClient::connect(&socket);
    client
        .stream
        .get_mut()
        .set_read_timeout(Some(std::time::Duration::from_secs(15)))
        .unwrap();
    client.initialize();
    let request = json!({"name":"plangraph_agent_spawn", "arguments":{
        "role":"review", "leg":"agy-cli", "writer_families":["openai"], "prompt":"review",
        "card":{"item_key":"demo:AGY", "estimate_tokens":1000, "head_sha":head}, "wait_seconds":5
    }});
    let missing_stamp = client.request("tools/call", request.clone());
    assert!(
        missing_stamp["error"]["message"]
            .as_str()
            .is_some_and(|s| s.contains("fast-checks stamp")),
        "{missing_stamp}"
    );
    let stamps = repo.join(".git/fast-checks");
    std::fs::create_dir(&stamps).unwrap();
    std::fs::write(stamps.join(&head), "green").unwrap();
    let reply = client.request("tools/call", request);
    let record = payload(&reply);
    assert_eq!(record["status"], "completed", "{reply}");
    assert_eq!(record["head_sha"], head);
    assert_eq!(record["item_key"], "demo:AGY");
    assert_eq!(record["witness_kind"], "cli_reported");
    assert_eq!(record["verdict"], "pass");
    assert_eq!(record["served_model"], "gemini-4-hydrogen-high");
}
