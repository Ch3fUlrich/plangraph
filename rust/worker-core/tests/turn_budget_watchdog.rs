use serde_json::{json, Value};
use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

#[test]
fn opt_in_reader_queues_one_line_and_under_budget_queues_none() {
    let dir = tempfile::tempdir().unwrap();
    let fleet = dir.path().join("fleet");
    fs::create_dir_all(fleet.join("status")).unwrap();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let status = |wake: u64| {
        json!({"turn_budget":{"session":"build-L1-0429",
        "role":"build-L1","session_id":"test-session","wake_calls":wake,
        "hour_calls":2,"calls":[now,now]}})
    };
    let path = fleet.join("status/test-session.json");
    fs::write(&path, status(7).to_string()).unwrap();
    let input = dir.path().join("snapshot.json");
    fs::write(
        &input,
        json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,
        "coding_vm_runs":0,"worker_idle":false,"host_drift":false,
        "closes_recent":0,"closes_previous":0})
        .to_string(),
    )
    .unwrap();
    let run = |state: &str, opt_in: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"));
        command.env_clear().arg(&input).arg(dir.path().join(state));
        if opt_in {
            command.env("PLANGRAPH_TURN_BUDGET_STATE_DIR", &fleet);
        }
        assert!(command.output().unwrap().status.success());
    };
    run("disabled", false);
    assert!(!dir.path().join("disabled/queue.json").exists());
    run("over", true);
    let queue_path = dir.path().join("over/queue.json");
    let queue: Value = serde_json::from_slice(&fs::read(&queue_path).unwrap()).unwrap();
    assert_eq!(queue.as_array().unwrap().len(), 1);
    assert_eq!(
        queue[0]["message"],
        "TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=2/60"
    );
    run("over", true);
    assert_eq!(
        queue,
        serde_json::from_slice::<Value>(&fs::read(&queue_path).unwrap()).unwrap()
    );
    fs::write(&path, status(6).to_string()).unwrap();
    run("under", true);
    assert!(!dir.path().join("under/queue.json").exists());
}
