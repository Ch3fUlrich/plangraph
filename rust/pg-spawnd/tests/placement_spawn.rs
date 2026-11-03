#![cfg(unix)]
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
    assert_eq!(reply["error"]["message"], "launch adapter could not start");
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
        if status == "completed" {
            assert_eq!(r["stdout_tail"], "hi\n");
            assert_eq!(r["stderr_tail"], "err\n");
            assert!(r["chosen_leg"].is_null());
        } else {
            assert!(r["chosen_leg"].is_null());
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
    waiter
        .stream
        .get_mut()
        .set_read_timeout(Some(std::time::Duration::from_secs(4)))
        .unwrap();
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
