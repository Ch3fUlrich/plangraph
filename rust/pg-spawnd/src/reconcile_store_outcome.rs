//! Attribution for the record-only settlement bridge.
use super::*;

fn key(leg: &Value, name: &str) -> Result<Option<String>, ReconcileError> {
    match &leg[name] {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        _ => Err(ReconcileError::Invalid),
    }
}

pub(super) fn missing(leg: &Value) -> &'static str {
    match (
        leg["leg_key"].is_null() && !leg["model"].is_string(),
        leg["route_key"].is_null(),
    ) {
        (true, true) => "leg_key and route_key unavailable",
        (true, false) => "leg_key unavailable",
        _ => "route_key unavailable",
    }
}

pub(super) fn build(
    leg: &Value,
    role: pg_worker_core::hosts::Role,
    run: &str,
    attempt: u32,
    now_ms: u64,
    duration: u64,
) -> Result<Option<crate::evidence_row::OutcomeRow>, ReconcileError> {
    if !leg.is_object() {
        return Err(ReconcileError::Invalid);
    }
    let leg_key = key(leg, "leg_key")?;
    let route_key = key(leg, "route_key")?;
    // src/plangraph/spawner/leg_probe.py:71, key(): bare agy models only.
    let leg_key = leg_key.or_else(|| {
        let model = leg["model"].as_str()?;
        Some(if leg["harness"] == "agy" && !model.contains('/') {
            format!("agy/{model}")
        } else {
            model.to_owned()
        })
    });
    // Python service writes asdict(Leg), with no route_key. No Python writer
    // or route-key algorithm exists: do not invent evidence attribution.
    let (Some(leg_key), Some(route_key)) = (leg_key, route_key) else {
        return Ok(None);
    };
    Ok(Some(crate::evidence_row::OutcomeRow {
        leg_key,
        route_key,
        ts: i64::try_from(now_ms / 1000).map_err(|_| ReconcileError::Invalid)?,
        role,
        outcome: "orphaned".into(),
        run_id: run.into(),
        attempt,
        duration_s: Some(i64::try_from(duration).map_err(|_| ReconcileError::Invalid)?),
        reset_at: None,
        reset_assumed: false,
        reset_clamped: false,
        open_tools_at_kill: None,
        s_eff_s: None,
        line: "reconciled group ended".into(),
    }))
}
