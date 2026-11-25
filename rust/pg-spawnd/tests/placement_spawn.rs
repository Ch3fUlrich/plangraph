#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
mod common;
#[path = "common/placement.rs"]
mod placement;
use placement::{payload, Fixture};
use serde_json::{json, Value};

#[test]
fn missing_launch_program_writes_no_record() {
    let mut f = Fixture::missing_launch();
    let reply = f.spawn(0.0);
    assert_eq!(reply["error"]["code"], -32603);
    let message = reply["error"]["message"].as_str().unwrap();
    assert!(
        message.starts_with("launch adapter could not start"),
        "{message}"
    );
    assert!(message.contains("os error"), "{message}");
    let runs = f.state.path().join("runs");
    assert!(!runs.exists() || std::fs::read_dir(runs).unwrap().next().is_none());
}

#[test]
fn real_run_and_failure() {
    for (command, status) in [("echo hi; echo err >&2", "completed"), ("exit 7", "failed")] {
        let mut f = Fixture::new(command, json!("payload"), &[]);
        let r = payload(f.spawn(5.0));
        assert_eq!(r["status"], status);
        assert!(r["job_id"].is_string());
        assert!(r["proc"].is_object());
        assert_eq!(r["writer_family"], "f");
        assert_eq!(r["warnings"], json!(["warning"]));
        assert_eq!(r["events"][0]["event"], "adapter");
        assert_eq!(
            r["attempts"][0],
            json!({
                "leg":r["plan"]["first_leg"], "status":status,
                "served_model":"unwitnessed", "served_family":"unwitnessed",
                "served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg",
                "request_side_model":null, "witness_kind":null, "provider_response":null,
                "skip_reason":if status == "completed" { Value::Null } else { r["reason"].clone() },
                "stdout_tail":r["stdout_tail"], "stderr_tail":r["stderr_tail"]
            })
        );
        assert_eq!(r["attempts"][0]["status"], status);
        assert_eq!(r["attempts"][0]["served_model"], "unwitnessed");
        if status == "completed" {
            assert_eq!(r["stdout_tail"], "hi\n");
            assert_eq!(r["stderr_tail"], "err\n");
            assert_eq!(r["chosen_leg"], r["plan"]["first_leg"]);
        } else {
            assert!(r["chosen_leg"].is_null());
            assert_eq!(r["attempts"][0]["skip_reason"], r["reason"]);
        }
        let dir = f
            .state
            .path()
            .join("runs")
            .join(r["run_id"].as_str().unwrap());
        for file in ["record.json", "live.log"] {
            if file == "live.log" && status == "failed" {
                continue; // No output means the lazy live log is not created.
            }
            let text = std::fs::read_to_string(dir.join(file)).unwrap();
            assert!(!text.contains(&["private", "-", "credential"].concat()));
        }
        let request: Value =
            serde_json::from_slice(&std::fs::read(f.state.path().join("request")).unwrap())
                .unwrap();
        assert_eq!(
            request,
            json!({
                "run_id":r["run_id"], "job_id":r["job_id"], "role":r["role"],
                "dry_run":false, "leg":r["plan"]["first_leg"],
                "writer_families":[], "review_of":null,
                "prompt":"verbatim\nprompt", "card":{"x":1},
                "cwd":std::env::current_dir().unwrap().to_string_lossy(),
                "state":f.state.path().to_string_lossy()
            })
        );
    }
}

#[test]
fn waiting_spawn_does_not_block_second_connection() {
    // serve uses a current-thread runtime; Fixture also pins Tokio's worker
    // count to one so an inline dispatch cannot hide on a second worker.
    let f = Fixture::new("/bin/sleep 2.5", Value::Null, &[]);
    let socket = f.state.path().join("socket");
    let mut waiter = common::McpClient::connect(&socket);
    waiter.set_response_timeout(std::time::Duration::from_secs(4));
    waiter.initialize();
    let thread = std::thread::spawn(move || {
        waiter.request(
            "tools/call",
            json!({
                "name":"plangraph_agent_spawn", "arguments":{
                    "prompt":"p", "card":{"x":1}, "wait_seconds":2
                }
            }),
        )
    });
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
    while !f.state.path().join("request").exists() {
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let began = std::time::Instant::now();
    let mut other = common::McpClient::connect(&socket);
    other.initialize();
    let reply = other.request(
        "tools/call",
        json!({"name":"plangraph_agent_health","arguments":{}}),
    );
    assert!(reply.get("result").is_some());
    assert!(began.elapsed() < std::time::Duration::from_secs(1));
    assert!(!thread.is_finished());
    thread.join().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(600));
}
#[test]
fn queue_full_and_nonblocking_wait() {
    let mut f = Fixture::new(
        "/bin/sleep 1",
        Value::Null,
        &["--max-runs", "1", "--max-queued", "0"],
    );
    let began = std::time::Instant::now();
    let first = payload(f.spawn(0.0));
    assert!(began.elapsed().as_secs_f64() < 1.0);
    assert!(["queued", "running"].contains(&first["status"].as_str().unwrap()));
    let refused = f.spawn(0.0);
    assert_eq!(refused["error"]["code"], -32000);
    let id = refused["error"]["data"]["run_id"].as_str().unwrap();
    let row: Value = serde_json::from_slice(
        &std::fs::read(f.state.path().join("runs").join(id).join("record.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(row["status"], "refused");
    assert_eq!(row["refusal"], "QueueFull");
    assert_eq!(row["exit_code"], 2);
    assert_eq!(row["error"], "run queue full");
    std::thread::sleep(std::time::Duration::from_millis(1100));
}
#[test]
fn dry_command_text() {
    for stdin in [Value::Null, json!("payload")] {
        let mut f = Fixture::new("echo hi", stdin.clone(), &[]);
        let r = payload(f.call("spawn", json!({"prompt":"p","card":{"x":1},"dry_run":true})));
        let suffix = if stdin.is_null() {
            " [stdin: 14 bytes]"
        } else {
            " [stdin: 7 bytes]"
        };
        assert_eq!(
            r["stdout_tail"],
            format!("[dry-run] Planned command: /bin/sh -c echo hi{suffix}")
        );
        assert_eq!(r["stdout_tail"], r["attempts"][0]["stdout_tail"]);
        assert!(r["pid"].is_null());
        assert!(r["job_id"].is_string());
    }
}

#[test]
fn launch_errors_do_not_create_records() {
    for dry in [false, true] {
        for (body, code, message) in [
            ("echo 'Spawn: denied' >&2; exit 2", -32000, "Spawn: denied"),
            ("exit 1", -32603, "launch adapter failed (exit 1)"),
            ("exit 124", -32603, "launch adapter failed (exit 124)"),
            ("", -32603, "launch adapter returned an invalid launch spec"),
        ] {
            let mut f = Fixture::new("exit 0", Value::Null, &[]);
            std::fs::write(
                f.state.path().join("launch"),
                format!("#!/bin/sh\ncat >/dev/null\n{body}\n"),
            )
            .unwrap();
            let reply = f.call("spawn", json!({"prompt":"p","card":{"x":1},"dry_run":dry}));
            assert_eq!(reply["error"]["code"], code, "{reply}");
            assert_eq!(reply["error"]["message"], message);
            assert!(!f.state.path().join("runs").exists());
        }
    }
}

#[test]
fn oversized_prompt_records_named_reason() {
    let mut f = Fixture::new("exit 0", Value::Null, &[]);
    std::fs::write(f.state.path().join("launch"),
        "#!/bin/sh\ncat >/dev/null\necho 'inline_diff_failed: prompt_too_large: request limit' >&2\nexit 2\n").unwrap();
    let row = payload(f.spawn(2.0));
    assert_eq!(row["status"], "failed");
    assert_eq!(row["reason"], "prompt_too_large");
    assert_eq!(row["exit_code"], 2);
    let saved = payload(f.call("result", json!({"run_id":row["run_id"]})));
    assert_eq!(saved["exit_code"], 2);
    assert_eq!(saved["reason"], "prompt_too_large");
    assert_eq!(saved["plan"]["first_leg"]["id"], "leg");
    assert_eq!(saved["attempts"][0]["status"], "failed");
    assert_eq!(saved["attempts"][0]["skip_reason"], "prompt_too_large");
    assert_eq!(saved["attempts"][0]["stderr_tail"], saved["stderr_tail"]);
    assert_eq!(
        saved["stderr_tail"],
        "inline_diff_failed: prompt_too_large: request limit"
    );
}

#[test]
fn malformed_prompt_transport_records_named_reason() {
    let mut f = Fixture::new("exit 0", Value::Null, &[]);
    std::fs::write(f.state.path().join("launch"),
        "#!/bin/sh\ncat >/dev/null\necho 'stdin_prompt_flag_missing: expected -p with an empty value' >&2\nexit 2\n").unwrap();
    let row = payload(f.spawn(2.0));
    assert_eq!(row["status"], "failed");
    assert_eq!(row["reason"], "stdin_prompt_flag_missing");
    assert_eq!(row["exit_code"], 2);
    let saved = payload(f.call("result", json!({"run_id":row["run_id"]})));
    assert_eq!(saved["reason"], "stdin_prompt_flag_missing");
    assert_eq!(saved["plan"]["first_leg"]["id"], "leg");
    assert_eq!(saved["attempts"][0]["status"], "failed");
    assert_eq!(
        saved["attempts"][0]["skip_reason"],
        "stdin_prompt_flag_missing"
    );
    assert_eq!(saved["attempts"][0]["stderr_tail"], saved["stderr_tail"]);
    assert_eq!(
        saved["stderr_tail"],
        "stdin_prompt_flag_missing: expected -p with an empty value"
    );
}

#[test]
fn inline_diff_exceeding_leg_limit_refuses_with_reason_without_launching() {
    registered_mimo_diff(10, None, false);
}

#[test]
fn mimo_inline_diff_respects_effective_request_limit() {
    registered_mimo_diff(10_000, Some(10), false);
    registered_mimo_diff(10_000, Some(1_000), true);
}

fn registered_mimo_diff(routed: u64, prepared: Option<u64>, fits: bool) {
    let f = Fixture::new("cat > observed_stdin", json!("review"), &[]);
    let repo = f.state.path().join("repo");
    std::fs::create_dir_all(&repo).unwrap();
    let run_git = |args: &[&str]| -> String {
        let out = std::process::Command::new("git")
            .current_dir(&repo)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{:?} {:?}",
            args,
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().into()
    };
    run_git(&["init", "-q"]);
    run_git(&["config", "user.name", "Fixture"]);
    run_git(&["config", "user.email", "fixture@example.invalid"]);
    std::fs::write(repo.join("change.txt"), "before\n").unwrap();
    run_git(&["add", "change.txt"]);
    run_git(&["commit", "-qm", "base"]);
    let base = run_git(&["rev-parse", "HEAD"]);
    std::fs::write(repo.join("change.txt"), "x".repeat(400) + "\n").unwrap();
    run_git(&["commit", "-qam", "head"]);
    let head = run_git(&["rev-parse", "HEAD"]);

    let leg = json!({
        "id": "mimo-v2.6-pro",
        "harness": "opencode",
        "model": "mimo/mimo-v2.6-pro",
        "family": "mimo",
        "tool_mode": "inline-only",
        "max_request_tokens": routed
    });
    let plan = json!({"legs": [leg.clone()], "first_leg": leg, "skipped": []});
    pg_worker_core::executable_test_support::write_executable(
        f.state.path().join("route"),
        format!(
            "#!/bin/sh\nprintf '%s' '{}'\n",
            plan.to_string().replace('\'', "'\\''")
        ),
    );
    let cfg_dir = f.state.path().join("xdg/plangraph");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    let hosts = json!({
        "version": 1,
        "self": "local",
        "hosts": [{
            "id": "local",
            "class": "worker",
            "aliases": [],
            "transport": "ssh",
            "os": "linux",
            "state_dir": f.state.path(),
            "caps": {"max_runs": 4, "max_writers": 2, "max_seats": 2, "max_sessions": 2, "mem_floor_mb": 1024},
            "warmup": {"warmup_s": 1, "ramp_mb": {"review": 0}},
            "session_mb": 256,
            "roles": ["review"],
            "repos": {
                "demo": {
                    "root": repo,
                    "worktrees_root": f.state.path().join("wt"),
                    "remote": "https://example.invalid/demo.git",
                    "import_roots": [],
                    "cache_env": {}
                }
            }
        }]
    });
    std::fs::write(cfg_dir.join("hosts.json"), hosts.to_string()).unwrap();

    if let Some(prepared) = prepared {
        let spec = json!({
            "argv": ["/bin/sh", "-c", "cat > observed_stdin"],
            "cwd": f.state.path(),
            "env": {},
            "prompt": "review",
            "stdin": "review",
            "max_request_tokens": prepared
        });
        pg_worker_core::executable_test_support::write_executable(
            f.state.path().join("launch"),
            format!("#!/bin/sh\ncat >/dev/null\nprintf '%s' '{spec}'\n"),
        );
    }
    let socket2 = f.state.path().join("socket-xdg");
    let mut cmd = std::process::Command::new(binary::spawnd());
    cmd.args(["serve", "--no-scope", "--socket"])
        .arg(&socket2)
        .arg("--state")
        .arg(f.state.path())
        .env("XDG_CONFIG_HOME", f.state.path().join("xdg"))
        .env("PG_SPAWND_ADAPTER", f.state.path().join("route"))
        .env("PG_SPAWND_LAUNCH_CMD", f.state.path().join("launch"));
    let child = cmd.spawn().unwrap();
    let _guard2 = common::ChildGuard { child };
    let mut client = common::McpClient::connect(&socket2);
    client.initialize();
    let reply = client.request(
        "tools/call",
        json!({
            "name": "plangraph_agent_spawn",
            "arguments": {
                "role": "review",
                "prompt": "review",
                "card": {"diff_ref": {"repo_id": "demo", "base_sha": base, "head_sha": head}},
                "wait_seconds": 2
            }
        }),
    );
    let row = payload(reply);
    let observed = f.state.path().join("observed_stdin");
    if fits {
        assert_eq!(row["status"], "completed", "{row}");
        let stdin = std::fs::read_to_string(observed).unwrap();
        assert!(stdin.contains("diff --git a/change.txt b/change.txt"));
        return;
    }
    assert_eq!(row["status"], "failed");
    assert_eq!(row["reason"], "prompt_too_large");
    assert_eq!(row["attempts"][0]["skip_reason"], "prompt_too_large");
    let err = row["error"].as_str().unwrap();
    assert!(
        err.contains("inline_diff_failed: prompt_too_large:")
            && err.contains("max_request_tokens 10"),
        "{err}"
    );
    assert!(!observed.exists());
}

#[test]
fn completed_review_persists_classification() {
    let mut fixture = Fixture::new("printf 'Findings without a decision\\n'", Value::Null, &[]);
    let run = payload(fixture.call(
        "spawn",
        json!({"card":{"x":1},"role":"review",
        "writer_families":["gpt"],"prompt":"review","wait_seconds":5}),
    ));
    assert_eq!(run["status"], "completed");
    assert_eq!(run["review_result"], "missing_verdict");
    let path = fixture
        .state
        .path()
        .join("runs")
        .join(run["run_id"].as_str().unwrap())
        .join("record.json");
    let saved: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(saved["review_result"], "missing_verdict");
}
