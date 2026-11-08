//! Time-bounded legacy recovery when no attempt identity can be verified.
use super::*;

pub(super) const UNKNOWN_IDENTITY_GRACE_MS: u64 = 10_000;

pub(super) fn needs_grace(row: &Entry) -> bool {
    row.identity
        .as_ref()
        .is_none_or(|p| p.pid == 0 && p.group.is_none() && p.start_time.is_none())
}

#[cfg(target_os = "linux")]
pub(super) fn pgid_gone(identity: &Proc) -> Result<bool, ReconcileError> {
    let pgid = identity.pgid.ok_or(ReconcileError::Unknown)?;
    if let Some(expected) = identity.start_time {
        match std::fs::read_to_string(format!("/proc/{pgid}/stat")) {
            Ok(stat) => {
                let start = stat
                    .rsplit_once(')')
                    .and_then(|(_, rest)| rest.split_whitespace().nth(19)?.parse::<u64>().ok())
                    .ok_or(ReconcileError::Unknown)?;
                if start != expected {
                    return Ok(true);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err(ReconcileError::Unknown),
        }
    }
    let pgid = i32::try_from(pgid)
        .ok()
        .filter(|id| *id > 0)
        .ok_or(ReconcileError::Unknown)?;
    if unsafe { libc::kill(-pgid, 0) } == 0 {
        return Ok(false);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Ok(true),
        Some(libc::EPERM) => Ok(false),
        _ => Err(ReconcileError::Unknown),
    }
}

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
    // Accepted legacy risk: an unverifiable identity (including a live pgid
    // without a leader start time) may release capacity while a child lives.
    // Use the same persisted deadline/started/mtime bound across restarts.
    record.set("attempts", json!(attempts));
    settle(root, &mut record, row.attempt, "unknown_identity", now)?;
    Ok(true)
}
