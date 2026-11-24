//! The one guard over live writer lanes: duplicate-assignment admission and the claim
//! heartbeat plan. Both read the same lane roster, so an item has one holder to name.
use crate::lane_roster::Lane;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::io::Write;
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Conflict {
    pub key: String,
    pub lane: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum GuardError {
    Held(Vec<Conflict>),
    OverrideNeedsReason,
}

impl std::fmt::Display for GuardError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Held(conflicts) => {
                f.write_str("lane_conflict:")?;
                for conflict in conflicts {
                    write!(f, " {} is held by {};", conflict.key, conflict.lane)?;
                }
                f.write_str(" pass a lane_override reason to admit a second lane")
            }
            Self::OverrideNeedsReason => f.write_str("lane_override requires a non-empty reason"),
        }
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Admission {
    /// The held items an authorized override admitted anyway, with the reason.
    pub overridden: Vec<Conflict>,
    pub reason: Option<String>,
}

/// Admit `keys` for the lane `own` unless another live lane already holds one of them.
/// `override_reason` is `Some` when the caller asks to admit a second lane; a blank reason is
/// refused even when nothing conflicts, so an override is never recorded without its reason.
pub fn admit(
    keys: &BTreeSet<String>,
    lanes: &[Lane],
    own: Option<&str>,
    override_reason: Option<&str>,
) -> Result<Admission, GuardError> {
    let reason = override_reason.map(str::trim);
    if reason == Some("") {
        return Err(GuardError::OverrideNeedsReason);
    }
    let conflicts: Vec<Conflict> = keys
        .iter()
        .flat_map(|key| {
            lanes
                .iter()
                .filter(|lane| {
                    own != Some(lane.name.as_str())
                        && own != Some(lane.detail.as_str())
                        && lane.keys.contains(key)
                })
                .map(|lane| Conflict {
                    key: key.clone(),
                    lane: lane.describe(),
                })
        })
        .collect();
    match (conflicts.is_empty(), reason) {
        (true, _) => Ok(Admission::default()),
        (false, None) => Err(GuardError::Held(conflicts)),
        (false, Some(reason)) => Ok(Admission {
            overridden: conflicts,
            reason: Some(reason.to_owned()),
        }),
    }
}

/// Append the recorded override (items, holding lanes, reason) to the private override log.
pub fn record_override(
    log: &Path,
    context: &str,
    admission: &Admission,
    now: u64,
) -> std::io::Result<()> {
    let Some(reason) = &admission.reason else {
        return Ok(());
    };
    let line = json!({
        "ts": now, "context": context, "reason": reason,
        "held": admission.overridden.iter()
            .map(|c| json!({"key": c.key, "lane": c.lane})).collect::<Vec<Value>>(),
    });
    if let Some(parent) = log.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)?;
    writeln!(file, "{line}")
}

/// One item of the orchestrator principal's keeper claims.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Claim {
    pub key: String,
    pub epoch: i64,
    pub state: String,
    pub claimed_by: String,
    /// Merged and waiting for its close pass: it needs no lease.
    pub merged: bool,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Plan {
    pub renew: Vec<(String, i64)>,
    pub skipped: Vec<(String, &'static str)>,
}

/// Renew exactly the claims `principal` holds on items a live lane holds.
pub fn heartbeat_plan(claims: &[Claim], principal: &str, lanes: &[Lane]) -> Plan {
    let mut plan = Plan::default();
    for claim in claims {
        let skip = if claim.state != "claimed" {
            Some("not_claimed")
        } else if claim.claimed_by != principal {
            Some("other_holder")
        } else if claim.merged {
            Some("merged")
        } else if !lanes.iter().any(|lane| lane.keys.contains(&claim.key)) {
            Some("no_live_lane")
        } else {
            None
        };
        match skip {
            Some(reason) => plan.skipped.push((claim.key.clone(), reason)),
            None => plan.renew.push((claim.key.clone(), claim.epoch)),
        }
    }
    plan
}

#[cfg(test)]
#[path = "lane_guard_tests.rs"]
mod tests;
