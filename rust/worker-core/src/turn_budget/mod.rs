//! Fail-open Claude Code call accounting and recognised-poll refusal.
mod poll;
mod storage;
mod transcript;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    path::{Path, PathBuf},
};

pub const DEFAULT_BUDGET: (u64, u64) = (6, 60);
/// Explicit role table; unknown roles start with the same conservative caps.
pub const ROLE_BUDGETS: &[(&str, u64, u64)] = &[
    ("build-L1", 6, 60),
    ("build-L2", 6, 60),
    ("review-L1", 6, 60),
    ("review-L2", 6, 60),
    ("planner", 6, 60),
    ("routing", 6, 60),
];

#[derive(Clone, Debug)]
pub struct Config {
    pub state_dir: PathBuf,
    pub launch_dir: PathBuf,
    pub session_name: Option<String>,
}
impl Config {
    pub fn from_env() -> Option<Self> {
        let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"));
        let state_dir = std::env::var_os("FLEET_STATE_DIR")
            .map(PathBuf::from)
            .or_else(|| {
                home.as_ref()
                    .map(|h| PathBuf::from(h).join(".local/state/fleet"))
            })?;
        let launch_dir = std::env::var_os("FLEET_LAUNCH_DIR")
            .map(PathBuf::from)
            .or_else(|| home.map(|h| PathBuf::from(h).join(".local/state/routing/launch")))
            .unwrap_or_default();
        Some(Self {
            state_dir,
            launch_dir,
            session_name: std::env::var("FLEET_SESSION_NAME").ok(),
        })
    }
}

#[derive(Default, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct BudgetStatus {
    pub session: String,
    pub session_id: String,
    pub role: String,
    pub wake_calls: u64,
    pub hour_calls: u64,
    /// Timestamps in the trailing, half-open hour (now - 3600, now].
    pub calls: Vec<u64>,
    seen: VecDeque<String>,
    transcript_path: String,
    offset: u64,
    polls: VecDeque<String>,
    reads: BTreeMap<String, storage::ReadState>,
}

pub fn safe_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

pub fn role(session: &str) -> &str {
    match session.rsplit_once('-') {
        Some((prefix, counter))
            if !counter.is_empty() && counter.bytes().all(|c| c.is_ascii_digit()) =>
        {
            prefix
        }
        _ => session,
    }
}

/// One line per over-budget session, even when both budgets are exceeded.
pub fn alert_line(status: &BudgetStatus, now: u64) -> Option<String> {
    if !safe_name(&status.session)
        || !safe_name(&status.role)
        || role(&status.session) != status.role
    {
        return None;
    }
    let (wake_cap, hour_cap) = ROLE_BUDGETS
        .iter()
        .find(|r| r.0 == status.role)
        .map(|r| (r.1, r.2))
        .unwrap_or(DEFAULT_BUDGET);
    let hours = status
        .calls
        .iter()
        .filter(|&&t| t <= now && now - t < 3600)
        .count() as u64;
    (status.wake_calls > wake_cap || hours > hour_cap).then(|| {
        format!(
            "TURN-BUDGET {} {} wake={}/{wake_cap} hour={hours}/{hour_cap}",
            status.role, status.session, status.wake_calls
        )
    })
}

/// Injectable watchdog reader; missing, corrupt and unrelated files are ignored.
pub fn read_alerts(state_dir: &Path, now: u64) -> Vec<String> {
    let Ok(entries) = fs::read_dir(state_dir.join("status")) else {
        return vec![];
    };
    let mut lines = BTreeMap::new();
    for entry in entries.flatten() {
        if entry.path().extension().is_none_or(|e| e != "json") {
            continue;
        }
        let Some(value) = storage::read_json(&entry.path()) else {
            continue;
        };
        let Ok(status) = serde_json::from_value::<BudgetStatus>(value["turn_budget"].clone())
        else {
            continue;
        };
        if let Some(line) = alert_line(&status, now) {
            lines.insert((status.role, status.session), line);
        }
    }
    lines.into_values().collect()
}

/// Only recognised polling produces a refusal; accounting/IO failures are silent.
pub fn handle(payload: &Value, config: &Config, now: u64) -> Option<String> {
    let event = payload["hook_event_name"].as_str().unwrap_or("");
    if !matches!(
        event,
        "UserPromptSubmit" | "Notification" | "PreToolUse" | "PostToolUse" | "Stop"
    ) {
        return None;
    }
    let command = (payload["tool_name"] == "Bash")
        .then(|| payload["tool_input"]["command"].as_str())
        .flatten();
    let static_poll = event == "PreToolUse" && command.is_some_and(poll::sleep_poll);
    if static_poll {
        // A denied attempt is still a model response. Count it when possible,
        // but never let malformed metadata/state turn a known poll into allow.
        if let Some(id) = payload["session_id"].as_str().filter(|s| safe_name(s)) {
            if !config.state_dir.as_os_str().is_empty() {
                let _ = update(payload, config, id, command, now);
            }
        }
        return Some(poll::refusal());
    }
    let id = payload["session_id"].as_str().filter(|s| safe_name(s))?;
    if config.state_dir.as_os_str().is_empty() {
        return None;
    }
    update(payload, config, id, command, now).ok().flatten()
}

fn update(
    payload: &Value,
    config: &Config,
    id: &str,
    command: Option<&str>,
    now: u64,
) -> Result<Option<String>, Box<dyn std::error::Error>> {
    let dir = config.state_dir.join("status");
    fs::create_dir_all(&dir)?;
    // Serialize cross-process read/modify/rename; contention is a fail-open no-op.
    let Some(_guard) = crate::lockfile::try_lock(&dir.join(format!("{id}.lock")))? else {
        return Ok(None);
    };
    let path = dir.join(format!("{id}.json"));
    let mut document = match storage::read_json(&path) {
        Some(v) if v.is_object() => v,
        _ if !path.exists() => serde_json::json!({}),
        _ => return Ok(None), // never overwrite somebody else's garbled status
    };
    let mut status: BudgetStatus = if document.get("turn_budget").is_some() {
        serde_json::from_value(document["turn_budget"].clone())?
    } else {
        BudgetStatus::default()
    };
    // Migrate old raw IDs to fixed-size hashes, and compact legacy histories
    // before ingestion so a near-cap file can resume accounting immediately.
    status.seen = status
        .seen
        .iter()
        .map(|id| transcript::message_key(id))
        .collect();
    status.seen.drain(..status.seen.len().saturating_sub(2048));
    status.polls.drain(..status.polls.len().saturating_sub(256));
    status.reads.retain(|key, _| status.polls.contains(key));
    let name = storage::resolve_name(config, id);
    status.session = name.clone();
    status.session_id = id.into();
    status.role = role(&name).into();
    // Finish accounting the previous wake before opening the next one.
    // Counter failures must not disable a recognised polling refusal.
    let _ = transcript::ingest(payload, &mut status, now);
    status.calls.retain(|&t| t <= now && now - t < 3600);
    status
        .calls
        .drain(..status.calls.len().saturating_sub(8192));
    status.hour_calls = status.calls.len() as u64;
    let event = payload["hook_event_name"].as_str().unwrap_or("");
    if matches!(event, "UserPromptSubmit" | "Notification") {
        status.wake_calls = 0;
    }
    let mut denied = false;
    if event == "PreToolUse" {
        if let Some(command) = command.filter(|s| poll::read_only(s)) {
            let key = poll::fingerprint(command, payload["cwd"].as_str().unwrap_or(""));
            let known = status.polls.contains(&key);
            let read = status.reads.entry(key.clone()).or_insert_with(|| {
                storage::read_scope(command, payload["cwd"].as_str().unwrap_or(""))
            });
            denied = known && !read.retry;
            read.retry = false;
            if !known {
                status.polls.push_back(key);
            }
        }
    }
    if event == "PostToolUse" && storage::successful(payload) {
        let changed = storage::changed_paths(payload, command);
        for read in status.reads.values_mut() {
            if read.affected_by(&changed) {
                read.retry = true;
            }
        }
        if let Some(command) = command.filter(|s| poll::read_only(s)) {
            let key = poll::fingerprint(command, payload["cwd"].as_str().unwrap_or(""));
            if let Some(read) = status.reads.get_mut(&key) {
                if let Some(output) = storage::output_key(&payload["tool_response"]) {
                    if read.output.as_ref().is_some_and(|old| old != &output) {
                        read.retry = true;
                    }
                    read.output = Some(output);
                }
            }
        }
    }
    while status.polls.len() > 256 {
        if let Some(key) = status.polls.pop_front() {
            status.reads.remove(&key);
        }
    }
    document["turn_budget"] = serde_json::to_value(&status)?;
    // Once identified, a repeated poll stays denied even if the advisory
    // counter's new status cannot be persisted.
    let written = storage::atomic_write(&path, &serde_json::to_vec(&document)?);
    if !denied {
        written?;
    }
    Ok(denied.then(poll::refusal))
}
