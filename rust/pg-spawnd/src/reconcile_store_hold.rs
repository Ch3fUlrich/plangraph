//! Durable, identity-independent uncertainty budget. Caller holds record.lock.
use super::{read, Entry, ReconcileError};
use serde_json::{json, Value};
use std::path::Path;

const UNKNOWN_IDENTITY_GRACE_MS: u64 = 10_000;

/// An empty scope cannot excuse an original leader that escaped confinement.
#[cfg(target_os = "linux")]
pub(super) fn leader_nonlive(identity: &super::Proc) -> Result<bool, ReconcileError> {
    match std::fs::read_to_string(format!("/proc/{}/stat", identity.pid)) {
        Ok(stat) => {
            let (_, rest) = stat.rsplit_once(')').ok_or(ReconcileError::Unknown)?;
            let fields: Vec<_> = rest.split_whitespace().collect();
            let start = fields
                .get(19)
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or(ReconcileError::Unknown)?;
            Ok(identity
                .start_time
                .is_some_and(|expected| expected != start)
                || fields.first().is_some_and(|s| ["Z", "X"].contains(s)))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(_) => Err(ReconcileError::Unknown),
    }
}

pub(super) fn retain(dir: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    let path = dir.join("reconcile.json");
    let now = u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| ReconcileError::Io)?
            .as_millis(),
    )
    .map_err(|_| ReconcileError::Io)?;
    let mut hold = match std::fs::symlink_metadata(&path) {
        Ok(_) => read(&path)?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({
            "run_id": row.run_id, "slot_id": row.run_id,
            "hold_started_ms": now,
            "hold_deadline_ms": now.saturating_add(UNKNOWN_IDENTITY_GRACE_MS),
            "effective_now_ms": now, "reason": "unproven",
            "disposition": "hold", "reservation": "retained", "outbox": []
        }),
        Err(_) => return Err(ReconcileError::Io),
    };
    let before = hold.clone();
    let started = hold["hold_started_ms"]
        .as_u64()
        .ok_or(ReconcileError::Invalid)?;
    let deadline = hold["hold_deadline_ms"]
        .as_u64()
        .ok_or(ReconcileError::Invalid)?;
    let effective = hold["effective_now_ms"]
        .as_u64()
        .ok_or(ReconcileError::Invalid)?;
    if hold["run_id"] != row.run_id || !hold["slot_id"].is_string() {
        return Err(ReconcileError::Invalid);
    }
    hold["observed_identity"] = row.identity.as_ref().map_or(
        Value::Null,
        |p| json!({"pid":p.pid,"start_time":p.start_time,"group":p.group,"pgid":p.pgid}),
    );
    hold["observed_attempt"] = json!(row.attempt);
    if hold["disposition"] != "unresolved" {
        hold["effective_now_ms"] = json!(now.max(effective));
        if now.max(effective) >= deadline {
            hold["disposition"] = json!("unresolved");
            let key = json!([row.run_id, hold["slot_id"], started]);
            let outbox = hold["outbox"]
                .as_array_mut()
                .ok_or(ReconcileError::Invalid)?;
            if !outbox.iter().any(|e| e["idempotency_key"] == key) {
                outbox.push(
                    json!({"event":"RECONCILE_UNRESOLVED", "idempotency_key":key,
                    "reason":"whole-group or original-leader proof unavailable"}),
                );
            }
        }
    }
    if hold != before || !path.exists() {
        pg_worker_core::run::record_io::atomic_write_private(
            &path,
            &serde_json::to_vec(&hold).map_err(|_| ReconcileError::Invalid)?,
        )
        .map_err(|_| ReconcileError::Io)?;
    }
    Ok(hold["disposition"] == "unresolved")
}
