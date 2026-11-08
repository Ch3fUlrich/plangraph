#![cfg(unix)]
mod common;
#[path = "common/placement.rs"]
mod placement;
use placement::{payload, Fixture};
use serde_json::{json, Value};

fn running(f: &mut Fixture, id: &Value) {
    let began = std::time::Instant::now();
    while payload(f.call("status", json!({"run_id":id})))["status"] != "running" {
        assert!(began.elapsed().as_secs_f64() < 2.0);
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}
#[test]
fn job_views_controls_and_health() {
    let mut f = Fixture::new("exec /bin/sleep 30", Value::Null, &[]);
    let health = payload(f.call("health", json!({})));
    assert_eq!(health["runner_jobs_total"], 0);
    assert_eq!(health["active_jobs_count"], 0);
    let mut r = payload(f.spawn(0.0));
    running(&mut f, &r["run_id"]);
    r = payload(f.call("status", json!({"run_id":r["run_id"]})));
    let health = payload(f.call("health", json!({})));
    assert_eq!(health["runner_jobs_total"], 1);
    assert_eq!(health["active_jobs_count"], 1);
    assert_eq!(health["placement"], "off");
    assert_eq!(health["catalog_models_count"], 0);
    assert_eq!(health["gateway_default_url"], "");
    let job = payload(f.call("status", json!({"job_id":r["job_id"]})));
    let mut keys: Vec<_> = job
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(keys, "command cwd dry_run end_time events exit_code job_id last_activity_ts model_or_combo request_side_model risk role served_family served_model served_witness_reason start_time status stderr stdout steer_messages task_prompt witness_kind".split_whitespace().collect::<Vec<_>>());
    assert_eq!(job["task_prompt"], "");
    assert_eq!(job["model_or_combo"], "m");
    assert_eq!(job["served_model"], "unwitnessed");
    assert_eq!(job["command"], json!([]));
    assert_eq!(payload(f.call("status", json!({})))["jobs"], json!([job]));
    assert_eq!(
        payload(f.call("steer", json!({"job_id":r["job_id"],"message":"hi"})))["error"],
        "stdin closing"
    );
    let began = std::time::Instant::now();
    let cancelled = payload(f.call("cancel", json!({"run_id":r["run_id"]})));
    assert!(began.elapsed().as_secs_f64() < 5.0);
    assert_eq!(cancelled["status"], "cancelled");
    assert_eq!(cancelled["exit_code"], 1);
    assert_eq!(cancelled["reason"], "cancelled");
    assert_eq!(
        payload(f.call("cancel", json!({"run_id":r["run_id"]}))),
        cancelled
    );
    assert_eq!(
        payload(f.call("cancel", json!({"job_id":r["job_id"]}))),
        json!({"job_id":r["job_id"],"status":"cancelled"})
    );
    assert_eq!(
        payload(f.call("steer", json!({"job_id":r["job_id"],"message":"hi"})))["error"],
        "no stdin"
    );
    let dry = payload(f.call("spawn", json!({"prompt":"p","card":{"x":1},"dry_run":true})));
    assert_eq!(
        payload(f.call("status", json!({"job_id":dry["job_id"]})))["dry_run"],
        true
    );
    let health = payload(f.call("health", json!({})));
    assert_eq!(health["runner_jobs_total"], 2);
    assert_eq!(health["active_jobs_count"], 0);
}
#[test]
fn cancel_errors_and_foreign_pid_are_safe() {
    let mut f = Fixture::new("true", Value::Null, &[]);
    for (args, message) in [
        (json!({}), "'job_id'"),
        (json!({"job_id":"x"}), "'Job x not found'"),
        (json!({"run_id":"run-missing"}), "unknown run id"),
    ] {
        let r = f.call("cancel", args);
        assert_eq!(r["error"]["code"], -32602);
        assert_eq!(r["error"]["message"], message);
    }
    let error = f.call("cancel", json!({"run_id":null,"job_id":"x"}));
    assert_eq!(error["error"]["code"], -32602);
    assert_eq!(error["error"]["message"], "unknown run id");
    let numeric = f.call("cancel", json!({"run_id":5}));
    assert_eq!(numeric["error"]["message"], "unknown run id");
    assert_eq!(
        f.call("status", json!({"job_id":"x"}))["error"]["message"],
        "'Job x not found'"
    );
    assert_eq!(
        payload(f.call("steer", json!({"job_id":"x","message":"hi"})))["error"],
        "unknown job"
    );
    let dir = f.state.path().join("runs/run-foreign");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("record.json"),
        json!({"run_id":"run-foreign","status":"running","pid":std::process::id()}).to_string(),
    )
    .unwrap();
    let r = f.call("cancel", json!({"run_id":"run-foreign"}));
    assert_eq!(r["error"]["code"], -32000);
    assert_eq!(
        r["error"]["message"],
        "run is not owned by this daemon; cancel it with the Python spawner"
    );
    assert_eq!(unsafe { libc::kill(std::process::id() as i32, 0) }, 0);
}
#[test]
fn queued_and_running_job_cancel() {
    let mut f = Fixture::new("exec /bin/sleep 30", Value::Null, &["--max-runs", "1"]);
    let mut first = payload(f.spawn(0.0));
    running(&mut f, &first["run_id"]);
    first = payload(f.call("status", json!({"run_id":first["run_id"]})));
    let queued = payload(f.spawn(0.0));
    assert_eq!(queued["status"], "queued");
    let jobs = payload(f.call("status", json!({})));
    let queued_job = jobs["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|j| j["status"] == "queued")
        .unwrap();
    assert_eq!(
        payload(f.call("cancel", json!({"job_id":queued_job["job_id"]})))["status"],
        "cancelled"
    );
    let row = payload(f.call("status", json!({"run_id":queued["run_id"]})));
    assert!(row["proc"].is_null());
    assert_eq!(
        payload(f.call("cancel", json!({"job_id":first["job_id"]}))),
        json!({"job_id":first["job_id"],"status":"cancelled"})
    );
}

#[test]
fn completed_cancel_preserves_record() {
    let mut f = Fixture::new("echo done", Value::Null, &[]);
    let record = payload(f.spawn(2.0));
    assert_eq!(record["status"], "completed");
    assert!(record["events"]
        .as_array()
        .unwrap()
        .iter()
        .any(|event| event["reason"] == "no_preflight_configured"));
    assert_eq!(
        payload(f.call("cancel", json!({"run_id":record["run_id"]}))),
        record
    );
}
