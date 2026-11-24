//! Duplicate-assignment guard shared by native submit and the Claude writer launch.
//!
//! An item a live lane already holds is refused with the holding lane named; a second lane
//! gets in only with a recorded `lane_override` reason (appended to `lane-overrides.jsonl`).
use pg_worker_core::lane_guard::{admit, record_override, GuardError};
use pg_worker_core::lane_roster::{item_keys, local_lanes, valid_key};
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

pub const OVERRIDE_LOG: &str = "lane-overrides.jsonl";

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

/// The items a submission names: its brief lines plus the card's `item_key` and `item_keys`.
pub fn submission_keys(prompt: &str, card: &Value) -> BTreeSet<String> {
    let mut keys = item_keys(prompt);
    let listed = card["item_keys"].as_array().into_iter().flatten();
    for key in [&card["item_key"]].into_iter().chain(listed) {
        keys.extend(key.as_str().filter(|k| valid_key(k)).map(str::to_owned));
    }
    keys
}

/// Refuse a submission that a live lane already holds. `state` holds the native runs,
/// `sessions` the Claude launch records; `context` names the entry point in the override log.
pub fn check(
    (state, sessions): (&Path, &Path),
    context: &str,
    keys: &BTreeSet<String>,
    override_reason: Option<&str>,
) -> Result<(), String> {
    if keys.is_empty() && override_reason.is_none() {
        return Ok(());
    }
    let now = unix_now();
    let lanes = local_lanes(state, sessions, now);
    let admission =
        admit(keys, &lanes, None, override_reason).map_err(|e: GuardError| e.to_string())?;
    record_override(&state.join(OVERRIDE_LOG), context, &admission, now)
        .map_err(|e| format!("cannot record the lane override: {e}"))
}

#[cfg(test)]
#[path = "lane_admission_tests.rs"]
mod tests;
