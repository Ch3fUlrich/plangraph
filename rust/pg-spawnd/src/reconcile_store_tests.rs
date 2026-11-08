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
    record.set("max_s", json!(-1));
    record.save().unwrap();
    assert!(inventory(&dir.0).unwrap().is_empty());
}

#[test]
fn reconcile_shadow_accepts_unknown_start_time_and_optional_group() {
    for proc in [
        json!({"pid":123,"start_time":null,"group":null}),
        json!({"pid":123}),
    ] {
        let dir = StateDir::new();
        let mut record = dir.record();
        record.set("proc", proc.clone());
        record.set("max_s", json!(7200)); // old records remain readable
        record.save().unwrap();
        assert!(crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).is_ok());
        record.set(
            "attempts",
            json!([{"status":"running", "started":1,
            "leg":{}, "proc":proc}]),
        );
        record.save().unwrap();
        assert!(crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).is_ok());
    }
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
    assert!(inventory(&dir.0).unwrap().is_empty());
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
    assert!(inventory(&dir.0).unwrap().is_empty());
}

#[cfg(target_os = "linux")]
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
    let diagnostics = crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).unwrap();
    assert!(diagnostics.is_empty());
    assert_eq!(before, std::fs::read(record.path()).unwrap());
    assert!(!dir.0.join("reconcile.lock").exists());
}

#[test]
fn reconcile_missing_identity_is_unknown_and_not_settled() {
    let dir = StateDir::new();
    let record = dir.record();
    let before = std::fs::read(record.path()).unwrap();
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert!(
        crate::reconcile::startup(&dir.0, Some("shadow"), &json!({}))
            .unwrap()
            .is_empty()
    );
    assert_eq!(before, std::fs::read(record.path()).unwrap());
}

#[path = "reconcile_store_cgroup_tests.rs"]
#[cfg(unix)]
mod cgroup_tests;

#[cfg(target_os = "linux")]
#[test]
fn terminal_or_absent_row_requires_settled_probe() {
    for status in ["running", "completed", "absent", "invalid"] {
        for settled in [false, true] {
            let dir = StateDir::new();
            let (_root, scope) = cgroup_tests::ScopeRoot::new(&dir.0);
            let mut record = dir.record();
            record.set(
                "proc",
                json!({"pid": 2_000_000_000u32, "start_time":null,
                "group":scope.to_str().unwrap(), "pgid":null}),
            );
            record.save().unwrap();
            let row = inventory(&dir.0).unwrap().remove(0);
            if status == "absent" {
                record.set("attempts", json!([{"status":"completed"}]));
            } else {
                record.set("status", json!(status));
            }
            record.save().unwrap();
            std::fs::write(
                scope.join("cgroup.events"),
                if settled {
                    "populated 0\n"
                } else {
                    "populated 1\n"
                },
            )
            .unwrap();
            std::fs::write(scope.join("cgroup.procs"), "").unwrap();
            let result = orphan_with(&dir.0, &row, &mut None);
            if settled && status != "invalid" {
                assert!(result.unwrap(), "{status}");
            } else {
                assert!(matches!(result, Err(ReconcileError::Unknown)), "{status}");
            }
        }
    }
}
