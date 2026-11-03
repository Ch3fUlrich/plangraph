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
