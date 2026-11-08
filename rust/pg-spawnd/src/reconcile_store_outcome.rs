//! Attribution for the record-only settlement bridge.
use super::*;

fn key(leg: &Value, name: &str) -> Result<Option<String>, ReconcileError> {
    match &leg[name] {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        _ => Err(ReconcileError::Invalid),
    }
}

pub(super) fn build(
    leg: &Value,
    role: pg_worker_core::hosts::Role,
    run: &str,
    attempt: u32,
    now_ms: u64,
    duration: u64,
) -> Result<Result<Value, &'static str>, ReconcileError> {
    // Attribution errors must never prevent durable process settlement.
    Ok(build_checked(leg, role, run, attempt, now_ms, duration)
        .unwrap_or(Err("invalid attribution")))
}
fn build_checked(
    leg: &Value,
    role: pg_worker_core::hosts::Role,
    run: &str,
    attempt: u32,
    now_ms: u64,
    duration: u64,
) -> Result<Result<Value, &'static str>, ReconcileError> {
    if !leg.is_object() {
        return Err(ReconcileError::Invalid);
    }
    let leg_key = key(leg, "leg_key")?;
    let route_key = key(leg, "route_key")?;
    // Python leg_probe.key(); checked by test_reconcile_leg_key_fixture.py.
    let leg_key = leg_key.or_else(|| {
        let model = leg["model"].as_str()?;
        Some(if leg["harness"] == "agy" && !model.contains('/') {
            format!("agy/{model}")
        } else {
            model.to_owned()
        })
    });
    // SpawnService._spawn's attempt writer (spawner/service.py) writes asdict(chosen),
    // served_model and served_family, but no route identity. Keep it null rather
    // than inventing attribution or losing a real orphan's outcome.
    let Some(leg_key) = leg_key else {
        return Ok(Err("leg_key unavailable"));
    };
    if leg_key.is_empty() || route_key.as_ref().is_some_and(|key| key.is_empty()) {
        return Ok(Err("leg_key or route_key unavailable"));
    }
    let row = json!({
        "leg_key": leg_key,
        "route_key": route_key,
        "ts": i64::try_from(now_ms / 1000).map_err(|_| ReconcileError::Invalid)?,
        "role": role,
        "outcome": "orphaned",
        "run_id": run,
        "attempt": attempt,
        "duration_s": i64::try_from(duration).map_err(|_| ReconcileError::Invalid)?,
        "reset_at": null,
        "reset_assumed": false,
        "reset_clamped": false,
        "open_tools_at_kill": null,
        "s_eff_s": null,
        "line": "reconciled group ended"
    });
    contract(&row)?;
    Ok(Ok(row))
}

/// The evidence-row contract for the written row. A null route_key (Python
/// writes no route identity) is checked under a non-empty stand-in so every
/// other field still meets the contract; the row itself keeps the null.
fn contract(row: &Value) -> Result<(), ReconcileError> {
    let mut checked = row.clone();
    if checked["route_key"].is_null() {
        checked["route_key"] = json!("unattributed");
    }
    let row: crate::evidence_row::OutcomeRow =
        serde_json::from_value(checked).map_err(|_| ReconcileError::Invalid)?;
    crate::evidence_row::validate(&crate::evidence_row::EvidenceRow::Outcome(row))
        .map_err(|_| ReconcileError::Invalid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_attribution_skips_row_instead_of_blocking_settlement() {
        for leg in [
            json!({"leg_key":"","route_key":"route"}),
            json!({"leg_key":"leg","route_key":""}),
            json!({"leg_key":42}),
            json!({"leg_key":"leg","route_key":[]}),
            json!(null),
            json!({}),
        ] {
            assert!(build(
                &leg,
                pg_worker_core::hosts::Role::Implement,
                "run",
                1,
                2000,
                1
            )
            .unwrap()
            .is_err());
            let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
            let mut record = crate::runs::create(
                &dir,
                "run-invalid",
                "fixture",
                json!({"role":"implement","attempts":[{"started":1,"leg":leg}]}),
                pg_worker_core::run::record::Status::Running,
            )
            .unwrap_or_else(|_| panic!("fixture creation failed"));
            super::super::settle(&dir, &mut record, 1, "gone", 2000).unwrap();
            let saved = pg_worker_core::run::record::RunRecord::read(&dir, "run-invalid").unwrap();
            assert_eq!(
                saved.status(),
                Some(pg_worker_core::run::record::Status::Orphaned)
            );
            let attempt = &saved.get("attempts").unwrap()[0];
            assert!(attempt["outcome"].is_null());
            assert!(attempt["outcome_skip_reason"].as_str().is_some());
            assert!(crate::evidence_row::adapted(&saved.to_value()).0.is_empty());
            std::fs::remove_dir_all(dir).unwrap();
        }
    }
}
