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
