//! Time-bounded legacy recovery when no attempt identity can be verified.
use super::*;

pub(super) const UNKNOWN_IDENTITY_GRACE_MS: u64 = 10_000;

pub(super) fn attempts(record: &Value) -> Result<Vec<Value>, ReconcileError> {
    match record.get("attempts") {
        None => Ok(Vec::new()),
        Some(value) => value.as_array().cloned().ok_or(ReconcileError::Invalid),
    }
}

pub(super) fn reclaim(root: &Path, run: &str) -> Result<(), ReconcileError> {
    pg_worker_core::slots::with_table(root, std::time::Duration::from_secs(2), |table| {
        table.active.retain(|slot| slot.run_id != run);
    })
    .map_err(|_| ReconcileError::Io)
}

// Caller holds state and record locks. Restarts never advance the time bound.
pub(super) fn settle_unknown(root: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    use pg_worker_core::run::record::RunRecord;
    let mut record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ReconcileError::Invalid)?
        .as_millis() as u64;
    let value = read(record.path())?;
    let mut attempts = attempts(&value)?;
    if attempts.is_empty() {
        attempts.push(json!({"status":"running", "started":record.get("started"),
            "leg":record.get("plan").and_then(|p| p.get("first_leg"))}));
    }
    let attempt = attempts
        .get(row.attempt as usize - 1)
        .ok_or(ReconcileError::Invalid)?;
    let base = match row.deadline_ms {
        Some(deadline) => deadline,
        None => match attempt.get("started").filter(|v| !v.is_null()) {
            Some(started) => seconds(started)?,
            None => std::fs::metadata(record.path())
                .and_then(|m| m.modified())
                .map_err(|_| ReconcileError::Io)?
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| ReconcileError::Invalid)?
                .as_millis() as u64,
        },
    };
    if now < base.saturating_add(UNKNOWN_IDENTITY_GRACE_MS) {
        return Err(ReconcileError::Unknown);
    }
    // Accepted legacy risk: without pid/group/pgid authority no process can be
    // probed; bounded settlement may release capacity while an unknown child lives.
    // Recorded pgids are retained in inventory and probed by gone(), not here.
    record.set("attempts", json!(attempts));
    settle(root, &mut record, row.attempt, "unknown_identity", now)?;
    Ok(true)
}
