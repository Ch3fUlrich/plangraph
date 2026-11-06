//! Derived rows only: legacy leg_events remain owned by the frozen adapter.
use crate::evidence::EvidenceError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", content = "row", rename_all = "snake_case")]
pub(crate) enum EvidenceRow {
    Outcome(OutcomeRow),
    Observation(ObservationRow),
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct OutcomeRow {
    pub leg_key: String,
    pub route_key: String,
    pub ts: i64,
    pub role: pg_worker_core::hosts::Role,
    pub outcome: String,
    pub run_id: String,
    pub attempt: u32,
    pub duration_s: Option<i64>,
    pub reset_at: Option<i64>,
    pub reset_assumed: bool,
    pub reset_clamped: bool,
    pub open_tools_at_kill: Option<u32>,
    pub s_eff_s: Option<i64>,
    #[serde(serialize_with = "serialize_line")]
    pub line: String,
}
const LINE_CAP: usize = 300;
fn serialize_line<S: serde::Serializer>(line: &str, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(
        &pg_worker_core::redact::redact(line)
            .chars()
            .take(LINE_CAP)
            .collect::<String>(),
    )
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ObservationKind {
    OmniRouteParked,
    OmniRouteSeen,
    OmniRouteStale,
    Canary,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Phase {
    First,
    Cleared,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ObservationRow {
    pub leg_key: String,
    pub ts: i64,
    pub kind: ObservationKind,
    pub until: Option<i64>,
    pub state: Option<String>,
    pub first_seen: Option<i64>,
    pub phase: Option<Phase>,
}
impl ObservationRow {
    pub fn key(&self) -> Result<String, EvidenceError> {
        use ObservationKind::*;
        let key = match self.kind {
            OmniRouteParked if self.until.is_some() => {
                serde_json::json!([self.leg_key, "parked", self.until.unwrap().div_euclid(60)])
            }
            OmniRouteParked | OmniRouteSeen => {
                let state = self
                    .state
                    .as_ref()
                    .filter(|s| !s.is_empty())
                    .ok_or(EvidenceError::Invalid)?;
                let first = self.first_seen.ok_or(EvidenceError::Invalid)?;
                if self.kind == OmniRouteSeen && self.phase.is_none() {
                    return Err(EvidenceError::Invalid);
                }
                if self.kind == OmniRouteSeen {
                    serde_json::json!([self.leg_key, self.kind, state, first, self.phase])
                } else {
                    serde_json::json!([self.leg_key, state, first])
                }
            }
            _ => serde_json::json!([self.leg_key, self.ts, self.kind]),
        };
        if self.leg_key.is_empty() {
            return Err(EvidenceError::Invalid);
        }
        Ok(key.to_string())
    }
}
pub(crate) fn validate(row: &EvidenceRow) -> Result<(), EvidenceError> {
    match row {
        EvidenceRow::Observation(o) => {
            if o.phase.is_some() && o.kind != ObservationKind::OmniRouteSeen {
                return Err(EvidenceError::Invalid);
            }
            o.key()?;
        }
        EvidenceRow::Outcome(o) => {
            let classes = [
                "OK",
                "HANG_NO_ACTIVITY",
                "HANG_SILENT",
                "NO_PROGRESS",
                "MAX_TIME",
                "KILLED_EXTERNAL",
                "EMPTY",
                "CRASH",
                "TOOL_CALL_ONLY",
                "NO_VERDICT",
                "MODEL_UNAVAILABLE",
                "PROVIDER_UNAVAILABLE",
                "QUOTA",
                "RATE_LIMIT",
                "AUTH",
                "caller_fault",
                "cancelled",
                "orphaned",
                "git_config_mutated",
                "writer_no_changes",
                "kill_incomplete",
                "proof_failure",
            ];
            if !classes.contains(&o.outcome.as_str())
                || !pg_worker_core::run::record::valid_run_id(&o.run_id)
                || o.leg_key.is_empty()
                || o.route_key.is_empty()
                || o.line.chars().count() > LINE_CAP
            {
                return Err(EvidenceError::Invalid);
            }
        }
    }
    Ok(())
}
/// Unknown/unclassified attempts are counted, never inferred from status.
pub(crate) fn adapted(record: &Value) -> (Vec<EvidenceRow>, usize) {
    let mut rows = Vec::new();
    let mut skipped = 0;
    for attempt in record["attempts"].as_array().into_iter().flatten() {
        match serde_json::from_value::<OutcomeRow>(attempt["outcome"].clone()) {
            Ok(row) if validate(&EvidenceRow::Outcome(row.clone())).is_ok() => {
                rows.push(EvidenceRow::Outcome(row))
            }
            _ => skipped += 1,
        }
    }
    (rows, skipped)
}
#[cfg(test)]
#[path = "evidence_row_tests.rs"]
mod tests;
