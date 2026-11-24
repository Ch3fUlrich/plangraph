//! The two fallback review seats (rule 3.66 f10): when the queued smart leg times out or is limited,
//! the seat is replaced by one seat on each required OpenCode leg, and the answer is one seat group.
use crate::errors::{RefusalKind, ToolError};
use crate::placement::Args;
use serde_json::{json, Value};

/// DeepSeek and GLM, the two weak seats; both must start or none does.
pub(crate) const LEGS: [&str; 2] = ["opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash"];

fn refused(message: String) -> ToolError {
    ToolError::Refused {
        kind: RefusalKind::Spawn,
        message,
        legs: vec![],
    }
}

/// One seat plan per fallback leg, taken from the route of the same request without the leg selector.
pub(crate) fn seat_plans(open_route: &Value) -> Result<Vec<Value>, ToolError> {
    let legs = open_route["legs"].as_array().map_or(&[][..], Vec::as_slice);
    LEGS.iter()
        .map(|id| {
            let leg = legs.iter().find(|l| l["id"] == *id).ok_or_else(|| {
                refused(format!(
                    "seat_fallback_unavailable: {id} is not in the review route; no fallback seat started"
                ))
            })?;
            let mut plan = open_route.clone();
            plan["first_leg"] = leg.clone();
            plan["legs"] = json!([leg]);
            Ok(plan)
        })
        .collect()
}

/// The run ids a seat launch answered with: one record, or a seat group when the seat split.
fn run_ids(answer: &Value) -> Result<Vec<String>, ToolError> {
    let ids: Vec<String> = match (answer["run_id"].as_str(), answer["seats"].as_array()) {
        (Some(id), _) => vec![id.to_owned()],
        (None, Some(seats)) => seats
            .iter()
            .filter_map(|seat| seat["run_id"].as_str().map(str::to_owned))
            .collect(),
        _ => vec![],
    };
    if ids.is_empty() {
        return Err(ToolError::Internal("fallback seat has no run id".into()));
    }
    Ok(ids)
}

/// What a fallback start left running: every started run id, and the failure that stopped the rest.
pub(crate) struct Started {
    pub ids: Vec<String>,
    pub failure: Option<ToolError>,
}

/// Starts both fallback seats through `start` (which launches one seat without waiting and answers
/// with its record). A launch that fails after another seat started stops there and keeps the started
/// run ids in `Started`; `Err` only when no seat could be planned, so nothing started.
pub(crate) fn start_seats(
    open_route: &Value,
    mut start: impl FnMut(Value) -> Result<Value, ToolError>,
) -> Result<Started, ToolError> {
    let mut started = Started {
        ids: vec![],
        failure: None,
    };
    for plan in seat_plans(open_route)? {
        match start(plan).and_then(|answer| run_ids(&answer)) {
            Ok(ids) => started.ids.extend(ids),
            Err(error) => {
                started.failure = Some(error);
                break;
            }
        }
    }
    Ok(started)
}

/// The request for a fallback seat: no leg selector, and no waiting (the group waits once).
pub(crate) fn seat_args(args: &Args) -> Args {
    let mut open = args.clone();
    open.remove("leg");
    open.remove(crate::seat_admission::TOKEN_FIELD);
    open
}

#[cfg(test)]
#[path = "seat_fallback_tests.rs"]
mod tests;
