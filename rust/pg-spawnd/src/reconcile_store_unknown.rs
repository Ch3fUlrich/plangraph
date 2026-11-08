//! Bounded legacy recovery when no attempt identity can be verified.
use super::*;

// Caller holds state and record locks. Count only durable restart observations.
pub(super) fn settle_unknown(root: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    use pg_worker_core::run::record::RunRecord;
    let mut record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ReconcileError::Invalid)?
        .as_millis() as u64;
    let mut attempts = record
        .get("attempts")
        .and_then(Value::as_array)
        .ok_or(ReconcileError::Invalid)?
        .clone();
    if attempts.is_empty() {
        attempts.push(json!({"status":"running", "started":record.get("started"),
            "leg":record.get("plan").and_then(|p| p.get("first_leg"))}));
    }
    let attempt = attempts
        .get_mut(row.attempt as usize - 1)
        .ok_or(ReconcileError::Invalid)?;
    let seen = attempt["unknown_identity_restarts"]
        .as_u64()
        .unwrap_or(0)
        .saturating_add(1);
    attempt["unknown_identity_restarts"] = json!(seen);
    record.set("attempts", json!(attempts));
    let expired = row
        .deadline_ms
        .and_then(|d| d.checked_add(10_000))
        .is_some_and(|d| now >= d);
    if !expired && (row.deadline_ms.is_some() || seen < 2) {
        record.save().map_err(|_| ReconcileError::Io)?;
        return Err(ReconcileError::Unknown);
    }
    settle(root, &mut record, row.attempt, "unknown_identity", now)?;
    // No process authority exists: reclaim only this now-terminal run's slots.
    pg_worker_core::slots::with_table(root, std::time::Duration::from_secs(2), |table| {
        table.active.retain(|slot| slot.run_id != row.run_id);
    })
    .map_err(|_| ReconcileError::Io)?;
    Ok(true)
}
