//! The two fallback review seats (rule 3.66 f10): when the queued smart leg times out or is limited,
//! the seat is replaced by one seat on each required OpenCode leg, and the answer is one seat group.
use crate::errors::{RefusalKind, ToolError};
use crate::placement::Args;
use serde_json::{json, Value};

/// Set on the request of a fallback seat. Only the spawner sets it, once: a fallback seat that cannot
/// start its own groups fails its group and never starts the fallback pair again.
pub(crate) const FLAG: &str = "seat_fallback";

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

/// A seat group answer that did not start every group leaves paths unreviewed: that is a failure of
/// the seat, whatever the runs it did start say.
fn uncovered(answer: &Value) -> Result<(), ToolError> {
    // `incomplete` is set by every failed seat group; the count alone can read zero when the run ids
    // outnumber the planned groups.
    let left = answer["unstarted_groups"].as_u64().unwrap_or(0);
    if left > 0 || answer["incomplete"] == json!(true) {
        return Err(refused(format!(
            "seat_fallback_incomplete: a fallback seat did not cover its paths ({left} group(s) unstarted): {}",
            answer["reason"].as_str().unwrap_or("no reason")
        )));
    }
    Ok(())
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
        let answer = match start(plan) {
            Ok(answer) => answer,
            Err(error) => {
                started.failure = Some(error);
                break;
            }
        };
        // A seat that split keeps every run it started, and its own failure to start the rest.
        match run_ids(&answer) {
            Ok(ids) => started.ids.extend(ids),
            Err(error) => {
                started.failure = Some(error);
                break;
            }
        }
        if let Err(error) = uncovered(&answer) {
            started.failure = Some(error);
            break;
        }
    }
    Ok(started)
}

/// The request for a fallback seat: no leg selector, no reservation claim, and (for a split review)
/// only the paths that no started group covers.
pub(crate) fn seat_args(args: &Args, paths: Option<&[String]>) -> Args {
    let mut open = args.clone();
    open.remove("leg");
    open.remove(crate::seat_admission::TOKEN_FIELD);
    open.insert(FLAG.into(), json!(true));
    if let Some(paths) = paths {
        if let Some(diff_ref) = open
            .get_mut("card")
            .and_then(|card| card.get_mut("diff_ref"))
            .and_then(Value::as_object_mut)
        {
            diff_ref.insert("paths".into(), json!(paths));
        }
    }
    open
}

/// Both fallback seats review `paths` (the whole request when `None`) next to the runs already
/// `started`; every run id stays in one seat group, and any failure to start or to cover the paths
/// fails that group instead of dropping it. `route` answers the open route of the request and `start`
/// launches one seat without waiting.
pub(crate) fn cover(
    state: &std::path::Path,
    args: &Args,
    started: &[String],
    total: usize,
    paths: Option<&[String]>,
    route: impl FnOnce(&Args) -> Result<Value, ToolError>,
    mut start: impl FnMut(&Args, Value) -> Result<Value, ToolError>,
) -> Result<Value, ToolError> {
    let group = format!(
        "seats-{}",
        &pg_worker_core::run::record::new_run_id()["run-".len()..]
    );
    let open = seat_args(args, paths);
    let mut seat = open.clone();
    seat.insert("wait_seconds".into(), json!(0));
    let mut ids = started.to_vec();
    let begun = route(&open).and_then(|route| start_seats(&route, |plan| start(&seat, plan)));
    let failure = match begun {
        Ok(begun) => {
            ids.extend(begun.ids);
            begun.failure
        }
        Err(error) => Some(error),
    };
    if let Some(error) = failure {
        return crate::review_split::unstarted_groups(state, &group, &ids, total, error);
    }
    crate::review_split::finish_group_seats(state, &group, &ids, args.get("wait_seconds"), false)
}

#[cfg(test)]
#[path = "seat_fallback_tests.rs"]
mod tests;
