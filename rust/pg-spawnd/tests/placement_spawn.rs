#![cfg(unix)]
mod common;
#[path = "common/placement.rs"]
mod placement;
use placement::{payload, Fixture};
use serde_json::{json, Value};

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
        assert_eq!(request["prompt"], "verbatim\nprompt");
        assert!(request.get("env").is_none());
        assert_eq!(request.as_object().unwrap().len(), 9);
    }
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
            ""
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
