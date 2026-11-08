use super::*;

#[test]
fn missing_started_uses_record_mtime_grace() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("started", Value::Null);
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(matches!(
        orphan_gone(&dir.0, &row),
        Err(ReconcileError::Unknown)
    ));
    let old = std::time::SystemTime::now()
        - std::time::Duration::from_millis(unknown::UNKNOWN_IDENTITY_GRACE_MS + 1000);
    std::fs::File::options()
        .write(true)
        .open(record.path())
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(old))
        .unwrap();
    assert!(orphan_gone(&dir.0, &row).unwrap());
}

#[test]
fn missing_attempts_synthetic_one_settles_after_time_grace() {
    let dir = StateDir::new();
    let record = dir.record();
    let mut value = read(record.path()).unwrap();
    value.as_object_mut().unwrap().remove("attempts");
    std::fs::write(record.path(), serde_json::to_vec(&value).unwrap()).unwrap();
    let rows = inventory(&dir.0).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].attempt, 1);
    assert!(orphan_gone(&dir.0, &rows[0]).unwrap());
    let saved = RunRecord::read(&dir.0, "run-test").unwrap();
    assert_eq!(saved.status(), Some(Status::Orphaned));
    assert_eq!(
        saved.get("attempts").unwrap()[0]["reason"],
        "unknown_identity"
    );
}

#[test]
fn restart_count_never_substitutes_for_elapsed_grace() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    record.set("started", json!(now));
    record.save().unwrap();
    for _ in 0..3 {
        let row = inventory(&dir.0).unwrap().remove(0);
        assert!(matches!(
            orphan_gone(&dir.0, &row),
            Err(ReconcileError::Unknown)
        ));
    }
    record.set(
        "started",
        json!(now - (unknown::UNKNOWN_IDENTITY_GRACE_MS / 1000 + 1) as f64),
    );
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(row.deadline_ms.is_none());
    assert!(orphan_gone(&dir.0, &row).unwrap());
}

#[test]
fn reclaim_failure_keeps_running_record_retryable() {
    let dir = StateDir::new();
    let record = dir.record();
    use pg_worker_core::slots;
    slots::with_table(&dir.0, std::time::Duration::from_secs(1), |table| {
        table.active.push(slots::ActiveSlot {
            run_id: "run-test".into(),
            pid: 0,
            start_time: 0,
            role: pg_worker_core::hosts::Role::Implement,
            class: slots::SlotClass::Other,
            repo_id: "test".into(),
            seat_slot: None,
            group: None,
            reservation_mb: 0,
            created_ms: 0,
            bound: true,
        });
    })
    .unwrap();
    // Block the atomic table write once, preserving the held slot on disk.
    let slots = dir.0.join("slots.json.tmp");
    std::fs::create_dir(&slots).unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(orphan_gone(&dir.0, &row).is_err());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Running)
    );
    std::fs::remove_dir(slots).unwrap();
    assert_eq!(
        slots::with_table(&dir.0, std::time::Duration::from_secs(1), |t| t
            .active
            .len())
        .unwrap(),
        1
    );
    assert!(orphan_gone(&dir.0, &row).unwrap());
    assert_eq!(
        slots::with_table(&dir.0, std::time::Duration::from_secs(1), |t| t
            .active
            .len())
        .unwrap(),
        0
    );
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Orphaned)
    );
    assert!(record.path().exists());
}

#[cfg(target_os = "linux")]
#[test]
fn pid_zero_with_recorded_live_pgid_is_not_settled() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("proc", json!({"pid":0,"pgid":unsafe { libc::getpgrp() }}));
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(row.identity.is_some());
    assert!(!orphan_gone(&dir.0, &row).unwrap());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Running)
    );
}
