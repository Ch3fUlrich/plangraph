//! Durable per-session state of the round watchdog: where it lives and how it is read.

use super::round_time::OpenWait;
use pg_worker_core::run::scrub::Scrubber;
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

/// The line the writer reads when a wait was ended; the second one says BLOCKED.
pub(super) fn notice_line(wait: &OpenWait, strikes: usize) -> String {
    let minutes = wait.waited_ms / 60_000;
    let first = format!(
        "Watchdog ended your background command `{}` (task {}) after {minutes} min of waiting; \
         the cap is {} min. Do not wait on one command that long: split it or run it in the \
         foreground with a timeout.",
        redacted(&wait.command),
        wait.task_id,
        super::round_watch::WAIT_LIMIT_MS / 60_000
    );
    if strikes >= super::round_watch::STRIKE_LIMIT {
        format!(
            "BLOCKED: second wait over the cap in this round, last command `{}`; this session \
             is ended. {first}",
            redacted(&wait.command)
        )
    } else {
        first
    }
}

/// Command text with credentials withheld, for every line that leaves the transcript:
/// the notice queued for delivery and the watchdog log. Environment values and
/// secret-shaped words are replaced by the shared redactor.
pub(super) fn redacted(text: &str) -> String {
    let env: Vec<_> = std::env::vars_os().collect();
    Scrubber::from_env(&env).scrub(text)
}
