//! Duplicate-assignment guard shared by native submit, the Claude writer launch and the
//! Python dispatch boundary (`pg-spawnd lane-check`).
//!
//! An item a live lane already holds is refused with the holding lane named; a second lane
//! gets in only with a recorded `lane_override` reason (appended to `lane-overrides.jsonl`).
//! The roster is native runs, reservations of submits in flight, Claude writer sessions that
//! `claude agents` still lists, and the keeper's open lanes (unmerged branches carrying the id).
use pg_worker_core::lane_guard::{admit, record_override, GuardError};
pub use pg_worker_core::lane_roster::live_claude_sessions_on;
use pg_worker_core::lane_roster::{
    branch_lanes, card_keys, item_keys, local_lanes, valid_key, write_reservation,
};
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

pub const OVERRIDE_LOG: &str = "lane-overrides.jsonl";
const LOCK_FILE: &str = "lane-admission.lock";

/// The fleet launcher's record directory of Claude sessions.
pub fn claude_sessions_dir() -> PathBuf {
    let state = std::env::var_os("ROUTING_STATE_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state/routing"))
        })
        .unwrap_or_default();
    state.join("launch/sessions")
}

pub fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// The items a submission names: its brief lines plus the card as the run record resolves it
/// (`item_key`, the `item` alias, `PLANGRAPH_SPAWN_ITEM`, every `item_keys` entry).
pub fn submission_keys(prompt: &str, card: &Value) -> BTreeSet<String> {
    let env_item = std::env::var("PLANGRAPH_SPAWN_ITEM").ok();
    let mut keys = item_keys(prompt);
    keys.extend(card_keys(card, env_item.as_deref()));
    keys
}

/// One admission decision.
pub struct Request<'a> {
    pub state: &'a Path,
    pub sessions: &'a Path,
    /// The keeper's open lanes (`work_exists.lanes` of the items; a closed pull request's
    /// branch is not among them). Only Python callers can read the keeper; others pass none.
    pub branches: &'a [String],
    pub context: &'a str,
    pub keys: &'a BTreeSet<String>,
    pub override_reason: Option<&'a str>,
    /// The lane being dispatched: never its own conflict.
    pub own: Option<&'a str>,
    /// Append an authorized override to the log (a preview never does).
    pub record: bool,
    /// Lists the live Claude sessions (`live_claude_sessions` in production).
    pub live: fn(Option<&str>) -> Option<BTreeSet<String>>,
    /// `(run id, lane name)`: hold the items for a submit that has not published a record yet.
    pub reserve: Option<(&'a str, &'a str)>,
}

/// Refuse a submission that a live lane already holds; admit it, record the override and
/// reserve the items otherwise. Check and reserve run under one lock, so two concurrent
/// submits cannot both pass.
pub fn check(request: &Request) -> Result<(), String> {
    if request.keys.is_empty() && request.override_reason.is_none() {
        return Ok(());
    }
    // Check, record and reserve are one decision under the admission lock; a lock that cannot
    // be taken refuses rather than letting two submits pass together.
    let _lock = if request.reserve.is_some() || request.record {
        Some(lock(request.state).map_err(|e| format!("cannot take the admission lock: {e}"))?)
    } else {
        None
    };
    let now = unix_now();
    let mut lanes = local_lanes(request.state, request.sessions, &request.live, now);
    lanes.extend(branch_lanes(request.branches.iter().map(String::as_str)));
    let own = request.reserve.map(|(id, _)| id).or(request.own);
    let admission = admit(request.keys, &lanes, own, request.override_reason)
        .map_err(|e: GuardError| e.to_string())?;
    if request.record {
        record_override(
            &request.state.join(OVERRIDE_LOG),
            request.context,
            &admission,
            now,
        )
        .map_err(|e| format!("cannot record the lane override: {e}"))?;
    }
    if let Some((run_id, name)) = request.reserve {
        write_reservation(request.state, run_id, name, request.keys, now)
            .map_err(|e| format!("cannot reserve the items: {e}"))?;
    }
    Ok(())
}

/// An exclusive advisory lock on the state directory's admission lock file, held until drop.
fn lock(state: &Path) -> std::io::Result<std::fs::File> {
    std::fs::create_dir_all(state)?;
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.join(LOCK_FILE))?;
    file.lock()?;
    Ok(file)
}

/// The canonical items of a `lane-check` request: the brief, the card as the run record
/// resolves it (including `PLANGRAPH_SPAWN_ITEM`), and any explicit `keys`.
fn request_keys(request: &Value) -> BTreeSet<String> {
    let mut keys = submission_keys(request["prompt"].as_str().unwrap_or(""), &request["card"]);
    keys.extend(
        request["keys"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|k| k.as_str().filter(|k| valid_key(k)).map(str::to_owned)),
    );
    keys
}

/// `pg-spawnd lane-keys`: the canonical items of a `lane-check` request as a JSON array, so
/// the Python caller asks the keeper about exactly the keys the guard will check.
pub fn keys_cli(input: &str) -> Result<String, String> {
    let request: Value =
        serde_json::from_str(input).map_err(|e| format!("lane-keys input: {e}"))?;
    serde_json::to_string(&request_keys(&request).into_iter().collect::<Vec<_>>())
        .map_err(|e| e.to_string())
}

/// `pg-spawnd lane-check`: the Python dispatch boundary and brief generator call this with one
/// JSON object on stdin (`state`, `prompt`, `card`, optional `repo`, `sessions`, `reason`,
/// `own`, `reserve` {run_id,name}, `record`, `context`). An error message means refused.
pub fn cli(input: &str) -> Result<(), String> {
    let request: Value =
        serde_json::from_str(input).map_err(|e| format!("lane-check input: {e}"))?;
    let text = |key: &str| request[key].as_str();
    let state = PathBuf::from(text("state").ok_or("lane-check needs state")?);
    let sessions = text("sessions").map_or_else(claude_sessions_dir, PathBuf::from);
    let keys = request_keys(&request);
    // A reservation takes the brief's lane name, else the run id.
    let lane_name = pg_worker_core::lane_roster::brief_name(text("prompt").unwrap_or(""));
    let branches: Vec<String> = request["branches"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|b| b.as_str().map(str::to_owned))
        .collect();
    let reserve = request["reserve"]["run_id"].as_str().map(|run_id| {
        let name = request["reserve"]["name"].as_str().or(lane_name.as_deref());
        (run_id, name.unwrap_or(run_id))
    });
    check(&Request {
        state: &state,
        sessions: &sessions,
        branches: &branches,
        context: text("context").unwrap_or("lane-check"),
        keys: &keys,
        override_reason: request
            .get("reason")
            .filter(|r| !r.is_null())
            .map(|r| r.as_str().unwrap_or("")),
        own: text("own"),
        live: live_claude_sessions_on,
        record: request["record"].as_bool().unwrap_or(true),
        reserve,
    })
}

#[cfg(test)]
#[path = "lane_admission_tests.rs"]
mod tests;
