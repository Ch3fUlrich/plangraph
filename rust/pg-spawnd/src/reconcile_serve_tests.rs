use super::*;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;

#[test]
fn unknown_scan_row_does_not_abort_later_rows() {
    let rows = [
        Err(ReconcileError::Unknown),
        Err(ReconcileError::Io),
        Err(ReconcileError::Invalid),
        Ok(false),
        Ok(true),
    ];
    let decisions = rows
        .into_iter()
        .map(scan_probe)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(decisions, vec![None, None, None, Some(false), Some(true)]);
}

struct StateDir(PathBuf);
impl StateDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn record(&self, id: &str) {
        RunRecord::create(
            &self.0,
            id,
            "local",
            json!({"role":"implement"}).as_object().unwrap().clone(),
            Status::Running,
            1.0,
        )
        .unwrap();
    }
}
impl Drop for StateDir {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn replay_backfill_failure_skips_row_and_processes_later_rows() {
    let state = StateDir::new();
    state.record("run-first");
    state.record("run-next");
    let mut calls = 0;
    let mut processed = 0;
    let result = replay_with(&state.0, |_| {
        calls += 1;
        if calls == 1 {
            // Model an unreadable or locked evidence row on the first backfill.
            return Err(ReconcileError::Io);
        }
        processed += 1;
        Ok(())
    });
    assert!(matches!(result, Err(ReconcileError::Unknown)));
    assert_eq!(calls, 2);
    assert_eq!(processed, 1);
}

#[test]
fn attach_locked_or_unreadable_row_marks_incomplete_and_continues() {
    let state = StateDir::new();
    for id in ["run-locked", "run-unreadable", "run-next"] {
        state.record(id);
    }
    let dir = state.0.join("runs/run-locked");
    let _lock = crate::reconcile_store::lock(&dir, &dir.join("record.lock")).unwrap();
    std::fs::write(state.0.join("runs/run-unreadable/record.json"), b"invalid").unwrap();
    let mut incomplete = false;
    let mut processed = Vec::new();
    for id in ["run-locked", "run-unreadable", "run-next"] {
        let Some((_lock, _record)) = attach_record(&state.0, id) else {
            incomplete = true;
            continue;
        };
        processed.push(id);
    }
    assert!(incomplete);
    assert_eq!(processed, ["run-next"]);
}

/// Daemon startup, with a really reaped process and a vanished scope, frees the promoted
/// reservation of a review run only when the slot carries the persisted role of its record.
#[cfg(target_os = "linux")]
#[test]
fn startup_frees_the_reservation_of_a_proven_gone_review_run_only_under_its_own_role() {
    use pg_worker_core::{
        hosts::Role,
        slots::{ActiveSlot, SlotClass},
    };
    use std::sync::{atomic::AtomicBool, Arc};
    let dir = StateDir::new();
    crate::reconcile_backend::native::TEST_ROOT
        .with(|value| *value.borrow_mut() = Some(dir.0.clone()));
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            crate::reconcile_backend::native::TEST_ROOT.with(|value| *value.borrow_mut() = None);
        }
    }
    let _reset = Reset;
    // A real process that started and was reaped; its scope directory never existed.
    let mut child = std::process::Command::new("sh")
        .args(["-c", "read token"])
        .stdin(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let stat = std::fs::read_to_string(format!("/proc/{}/stat", child.id())).unwrap();
    let started: u64 = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let pid = child.id();
    child.kill().unwrap();
    child.wait().unwrap();
    // SAFETY: read-only query of the effective user id.
    let euid = unsafe { libc::geteuid() };
    let mut table = slots::SlotTable::default();
    for (id, slot_role) in [("run-test", Role::Review), ("run-other", Role::Implement)] {
        let group = dir
            .0
            .join(format!("user.slice/user-{euid}.slice/pg-{id}.scope"));
        let mut record = RunRecord::create(
            &dir.0,
            id,
            "local",
            json!({"role":"review"}).as_object().unwrap().clone(),
            Status::Running,
            1.0,
        )
        .unwrap();
        record.set(
            "attempts",
            json!([{"status":"running", "started":1.0,
                "proc":{"pid":pid, "start_time":started, "group":group, "pgid":pid},
                "leg":{"leg_key":"leg", "route_key":"route"}}]),
        );
        record.save().unwrap();
        table.active.push(ActiveSlot {
            run_id: id.into(),
            pid,
            start_time: started,
            role: slot_role,
            class: SlotClass::Other,
            repo_id: "repo".into(),
            seat_slot: None,
            group: Some(group.to_string_lossy().into_owned()),
            reservation_mb: 0,
            created_ms: 1,
            bound: true,
        });
    }
    slots::with_table(&dir.0, Duration::from_secs(2), |t| *t = table.clone()).unwrap();
    let mut guard = Guard {
        stop: Arc::new(AtomicBool::new(false)),
        thread: None,
        _owner: None,
    };
    start(&dir.0, &mut guard).unwrap();
    drop(guard);
    let left = slots::with_table(&dir.0, Duration::from_secs(2), |t| t.clone()).unwrap();
    // The review reservation under the review role is freed; the implement-tagged one for
    // the same kind of record stays held.
    let held: Vec<&str> = left.active.iter().map(|a| a.run_id.as_str()).collect();
    assert_eq!(held, ["run-other"], "{left:?}");
    for id in ["run-test", "run-other"] {
        assert_eq!(
            RunRecord::read(&dir.0, id).unwrap().status(),
            Some(Status::Orphaned)
        );
    }
}
