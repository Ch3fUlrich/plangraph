use super::*;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;

struct StateDir(std::path::PathBuf);
impl StateDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn record(&self) -> RunRecord {
        RunRecord::create(
            &self.0,
            "run-test",
            "local",
            json!({
                "role":"implement", "plan":{"first_leg":{"leg_key":"leg", "route_key":"route"}}
            })
            .as_object()
            .unwrap()
            .clone(),
            Status::Running,
            1.0,
        )
        .unwrap()
    }
}
impl Drop for StateDir {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn reconcile_inventory_legacy_missing_proc_and_limits() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let rows = inventory(&dir.0).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].attempt, 1);
    assert!(rows[0].identity.is_none());
    assert_eq!(rows[0]._deadline_ms, 1_801_000);
    record.set("max_s", json!(-1));
    record.save().unwrap();
    assert!(inventory(&dir.0).is_err());
}

#[test]
fn reconcile_settle_one_outcome_one_event_null_exit() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "attempts",
        json!([{"status":"running", "started":1,
        "leg":{"leg_key":"leg", "route_key":"route"}}]),
    );
    settle(&dir.0, &mut record, 1, "ended_unobserved", 2000).unwrap();
    settle(&dir.0, &mut record, 1, "ended_unobserved", 3000).unwrap();
    assert_eq!(record.get("status").unwrap(), "orphaned");
    assert!(record.get("exit_code").unwrap().is_null());
    assert_eq!(record.get("ended").unwrap(), 2.0);
    assert_eq!(record.get("events").unwrap().as_array().unwrap().len(), 1);
    let attempts = record.get("attempts").unwrap();
    assert_eq!(attempts[0]["outcome"]["outcome"], "orphaned");
    assert_eq!(attempts[0]["outcome"]["ts"], 2);
}

#[test]
fn reconcile_inventory_ignores_terminal_and_refuses_mismatch() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("run_id", json!("run-other"));
    record.save().unwrap();
    assert!(inventory(&dir.0).is_err());
    record.set("run_id", json!("run-test"));
    record.transition(Status::Orphaned, 2.0).unwrap();
    record.save().unwrap();
    assert!(inventory(&dir.0).unwrap().is_empty());
}

#[cfg(unix)]
#[test]
fn reconcile_inventory_refuses_symlink_record() {
    let dir = StateDir::new();
    let record = dir.record();
    let moved = dir.0.join("saved.json");
    std::fs::rename(record.path(), &moved).unwrap();
    std::os::unix::fs::symlink(moved, record.path()).unwrap();
    assert!(inventory(&dir.0).is_err());
}

#[cfg(unix)]
#[test]
fn reconcile_crashed_daemon_real_child_inventory_is_read_only() {
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut daemon = Command::new("sh")
        .args([
            "-c",
            "sh -c 'exit 0' & child=$!; wait \"$child\"; echo ready; read token",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut ready = String::new();
    BufReader::new(daemon.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    daemon.kill().unwrap();
    daemon.wait().unwrap();
    assert_eq!(ready, "ready\n");
    record.set(
        "proc",
        json!({"pid":daemon.id(),"start_time":1,"group":null}),
    );
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    assert!(crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).is_ok());
    assert_eq!(before, std::fs::read(record.path()).unwrap());
    assert!(!dir.0.join("reconcile.lock").exists());
}
