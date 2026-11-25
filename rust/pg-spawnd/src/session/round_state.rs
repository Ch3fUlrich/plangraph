//! Durable per-session state of the round watchdog: where it lives and how it is read.

use serde_json::{json, Value};
use std::{collections::HashMap, fs, path::Path};

pub(super) fn read_state(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({"actions": {}}))
}

pub(super) fn closed_rounds_of(state: &Value) -> HashMap<String, i64> {
    let blocked = &state["blocked"];
    match (blocked["round_key"].as_str(), blocked["closed_ms"].as_i64()) {
        (Some(key), Some(at)) => HashMap::from([(key.to_owned(), at)]),
        _ => HashMap::new(),
    }
}

/// Rounds the watchdog closed, with their terminal time: the reader books nothing later.
pub fn closed_rounds(state_path: &Path) -> HashMap<String, i64> {
    closed_rounds_of(&read_state(state_path))
}

/// The durable BLOCKED outcome of a session's round, if the watchdog wrote one.
pub fn blocked_outcome(state_path: &Path) -> Option<Value> {
    let state = read_state(state_path);
    state["blocked"]
        .is_object()
        .then(|| state["blocked"].clone())
}

/// The action file of one session under a spawn state directory.
pub fn state_file(spawn_state: &Path, session: &str) -> std::path::PathBuf {
    spawn_state
        .join("round-watch")
        .join(format!("{session}.json"))
}

/// Spawn state directory, resolved as the watchdog and the session store resolve it.
pub fn spawn_state_dir() -> std::path::PathBuf {
    if let Some(dir) = std::env::var_os("PLANGRAPH_SPAWN_STATE").filter(|v| !v.is_empty()) {
        return dir.into();
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::home_dir().map(|home| home.join(".local/state")))
        .unwrap_or_default();
    base.join("plangraph")
}
