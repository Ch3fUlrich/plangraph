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

/// A launched review run whose process is proven gone frees its promoted reservation; the
/// slot must carry the role of the persisted record, or startup keeps it held forever.
#[cfg(target_os = "linux")]
#[test]
fn startup_frees_the_reservation_of_a_proven_gone_review_run_only_under_its_own_role() {
    use pg_worker_core::{
        hosts::Role,
        slots::{ActiveSlot, SlotClass},
    };
    let dir = StateDir::new();
    let group = "/sys/fs/cgroup/user.slice/pg-run-review.scope";
    let mut record = RunRecord::create(
        &dir.0,
        "run-review",
        "local",
        json!({"role":"review"}).as_object().unwrap().clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    record.set(
        "attempts",
        json!([{"status":"running", "started":1.0,
            "proc":{"pid":4242, "start_time":77, "group":group, "pgid":4242},
            "leg":{"leg_key":"leg", "route_key":"route"}}]),
    );
    record.save().unwrap();
    let rows = crate::reconcile_store::inventory(&dir.0).unwrap();
    assert_eq!(rows[0].role, "review");
    let slot = |role| ActiveSlot {
        run_id: "run-review".into(),
        pid: 4242,
        start_time: 77,
        role,
        class: SlotClass::Other,
        repo_id: "repo".into(),
        seat_slot: None,
        group: Some(group.into()),
        reservation_mb: 0,
        created_ms: 1,
        bound: true,
    };
    // A reservation tagged implement does not match the review record: it is never freed.
    let mut table = slots::SlotTable::default();
    table.active.push(slot(Role::Implement));
    assert!(validated_slots(&rows, &table).is_empty());
    // The promoted review reservation matches, and the startup reclaim frees exactly it.
    table.active = vec![slot(Role::Review)];
    let gone = validated_slots(&rows, &table);
    assert_eq!(gone.len(), 1);
    slots::with_table(&dir.0, Duration::from_secs(2), |t| *t = table.clone()).unwrap();
    reclaim_after(
        &dir.0,
        Reconciled {
            root: dir.0.clone(),
            gone,
        },
        1,
    )
    .unwrap();
    let left = slots::with_table(&dir.0, Duration::from_secs(2), |t| t.clone()).unwrap();
    assert!(left.active.is_empty(), "{left:?}");
}
