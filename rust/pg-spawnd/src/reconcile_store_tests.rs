use super::*;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;
#[cfg(target_os = "linux")]
#[path = "reconcile_store_identity_tests.rs"]
mod identity_tests;
#[cfg(target_os = "linux")]
#[path = "reconcile_store_pgid_tests.rs"]
mod pgid_tests;
#[path = "reconcile_store_recovery_tests.rs"]
mod recovery_tests;

struct StateDir(std::path::PathBuf);

#[test]
fn orphan_gone_uses_running_rows_missing_identity() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "attempts",
        json!([{"status":"running","leg":{"leg_key":"leg"}}]),
    );
    record.save().unwrap();
    let row = Entry {
        run_id: "run-test".into(),
        role: "implement".into(),
        attempt: 1,
        identity: None,
        deadline_ms: Some(u64::MAX - 10_000),
    };
    assert!(matches!(
        orphan_gone(&dir.0, &row),
        Err(ReconcileError::Unknown)
    ));
    let row = Entry {
        deadline_ms: Some(1),
        ..row
    };
    assert!(orphan_gone(&dir.0, &row).unwrap());
}
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
fn reconcile_settle_python_record_without_keys() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "attempts",
        json!([{"status":"running", "started":1,
        "leg":python_leg("agy", "sample-model")} ]),
    );
    settle(&dir.0, &mut record, 1, "gone", 2000).unwrap();
    settle(&dir.0, &mut record, 1, "gone", 3000).unwrap();
    let saved = RunRecord::read(&dir.0, "run-test").unwrap();
    assert_eq!(saved.status(), Some(Status::Orphaned));
    let entry = &saved.get("attempts").unwrap()[0];
    assert_eq!(entry["outcome"]["leg_key"], "agy/sample-model");
    assert!(entry["outcome"]["route_key"].is_null());
    assert!(entry.get("outcome_skip_reason").is_none());
    assert_eq!(saved.get("events").unwrap()[0]["event"], "RUN_ORPHANED");
    assert_eq!(saved.get("events").unwrap().as_array().unwrap().len(), 1);
}

#[test]
fn reconcile_settle_derives_python_leg_key() {
    for fixture in leg_fixtures().as_array().unwrap() {
        let row = outcome::build(
            &fixture["leg"],
            pg_worker_core::hosts::Role::Implement,
            "run-test",
            1,
            2000,
            1,
        )
        .unwrap()
        .unwrap();
        assert_eq!(row["leg_key"], fixture["expected"]);
        assert!(row["route_key"].is_null());
    }
}

#[test]
fn reconcile_settle_outcome_row_meets_evidence_contract() {
    let role = pg_worker_core::hosts::Role::Implement;
    let leg = json!({"harness": "agy", "model": "m", "route_key": "route"});
    let row = outcome::build(&leg, role, "run-test", 1, 2000, 1).unwrap();
    assert_eq!(row.unwrap()["route_key"], "route");
    for leg in [leg, json!({"harness": "agy", "model": "m"})] {
        let bad = outcome::build(&leg, role, "bad run id", 1, 2000, 1);
        assert!(bad.unwrap().is_err());
    }
}

#[test]
fn reconcile_settle_no_attempts_still_invalid() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("attempts", json!([]));
    assert!(matches!(
        settle(&dir.0, &mut record, 1, "gone", 2000),
        Err(ReconcileError::Invalid)
    ));
    assert_eq!(record.status(), Some(Status::Running));
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
    let mut record = dir.record();
    record.set(
        "started",
        json!(std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()),
    );
    record.save().unwrap();
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    let before = std::fs::read(record.path()).unwrap();
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
    for status in ["running", "completed", "absent", "missing", "invalid"] {
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
            if status == "missing" {
                // judge V8: no record.json at settle time takes the absent-record path.
                std::fs::remove_file(record.path()).unwrap();
            } else if status == "absent" {
                record.set("attempts", json!([{"status":"completed"}]));
            } else {
                record.set("status", json!(status));
            }
            if status != "missing" {
                record.save().unwrap();
            }
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
            if (settled || status == "running") && status != "invalid" {
                assert!(result.unwrap(), "{status}");
            } else {
                assert!(matches!(result, Err(ReconcileError::Unknown)), "{status}");
            }
        }
    }
}

fn leg_fixtures() -> Value {
    serde_json::from_str(include_str!("../tests/fixtures/reconcile_legs.json")).unwrap()
}
fn python_leg(harness: &str, model: &str) -> Value {
    leg_fixtures()
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["leg"]["harness"] == harness && row["leg"]["model"] == model)
        .unwrap()["leg"]
        .clone()
}

#[test]
fn reconcile_settle_skips_malformed_leg_and_keys() {
    for leg in [
        Value::Null,
        json!(3),
        json!({"leg_key":3}),
        json!({"route_key":3}),
        json!({"leg_key":false}),
        json!({"route_key":[]}),
    ] {
        let dir = StateDir::new();
        let mut record = dir.record();
        record.set(
            "attempts",
            json!([{"status":"running","started":1,"leg":leg}]),
        );
        settle(&dir.0, &mut record, 1, "gone", 2000).unwrap();
        assert_eq!(record.status(), Some(Status::Orphaned));
        assert!(record.get("attempts").unwrap()[0]["outcome"].is_null());
    }
}

#[test]
fn reconcile_skip_reason_and_crate_live_log() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "attempts",
        json!([{"status":"running","started":1,"leg":{}}]),
    );
    settle(&dir.0, &mut record, 1, "gone", 2000).unwrap();
    let saved = RunRecord::read(&dir.0, "run-test").unwrap();
    assert!(saved.get("attempts").unwrap()[0]["outcome_skip_reason"].is_string());
    assert_eq!(
        saved.get("attempts").unwrap()[0]["outcome_skip_reason"],
        "leg_key unavailable"
    );
    let log = std::fs::read_to_string(record.path().with_file_name("live.log")).unwrap();
    assert!(log.contains("run_id=run-test attempt=1 leg_key unavailable"));
    assert!(!log.contains("route_key unavailable"));
}
