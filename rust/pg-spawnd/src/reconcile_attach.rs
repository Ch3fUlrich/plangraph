//! Durable attachment marker, independent of the daemon startup loop.
use crate::reconcile::ReconcileError;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::{json, Value};

pub(crate) fn attach(record: &mut RunRecord, attempt: u32) -> Result<(), ReconcileError> {
    let invalid = || ReconcileError::Invalid;
    if record.status() != Some(Status::Running) {
        return Err(invalid());
    }
    let run = record
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|id| pg_worker_core::run::record::valid_run_id(id))
        .ok_or_else(invalid)?
        .to_owned();
    let mut attempts = record
        .get("attempts")
        .and_then(Value::as_array)
        .ok_or_else(invalid)?
        .clone();
    if attempts.is_empty() && attempt == 1 {
        attempts.push(json!({"status":"running", "proc":record.get("proc"),
            "started":record.get("started"), "max_s":record.get("max_s"),
            "leg":record.get("plan").and_then(|p| p.get("first_leg"))}));
    }
    let entry = attempts
        .get_mut(attempt.checked_sub(1).ok_or_else(invalid)? as usize)
        .filter(|v| v["status"] == "running" && v["proc"].is_object())
        .ok_or_else(invalid)?;
    entry["recovery_initialization"] = json!(true);
    let key = json!([run, attempt, "RUN_REATTACHED"]);
    let exists = record
        .get("events")
        .and_then(Value::as_array)
        .is_some_and(|events| events.iter().any(|e| e["idempotency_key"] == key));
    record.set("attempts", json!(attempts));
    if !exists {
        record
            .push_event(json!({"event":"RUN_REATTACHED", "attempt":attempt,
            "idempotency_key":key}))
            .map_err(|_| invalid())?;
    }
    record.save().map_err(|_| ReconcileError::Io)
}
