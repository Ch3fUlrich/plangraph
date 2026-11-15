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

/// Only recognised polling produces a refusal; accounting/IO failures go to stderr.
pub fn handle(payload: &Value, config: &Config, now: u64) -> Option<String> {
    handle_with_diagnostics(payload, config, now, |error| {
        eprintln!("turn-budget-hook: status update failed: {error}");
    })
}

/// Preserve fail-open library policy while letting the binary signal IO failure.
pub fn handle_with_diagnostics(
    payload: &Value,
    config: &Config,
    now: u64,
    mut report: impl FnMut(&dyn std::error::Error),
) -> Option<String> {
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
    let mut run_update = |id| match update(payload, config, id, command, now, &mut report) {
        Ok(denied) => denied,
        Err(error) => {
            report(error.as_ref());
            None
        }
    };
    if static_poll {
        // A denied attempt is still a model response. Count it when possible,
        // but never let malformed metadata/state turn a known poll into allow.
        if let Some(id) = payload["session_id"].as_str().filter(|s| safe_name(s)) {
            if !config.state_dir.as_os_str().is_empty() {
                run_update(id);
            }
        }
        return Some(poll::refusal());
    }
    let id = payload["session_id"].as_str().filter(|s| safe_name(s))?;
    if config.state_dir.as_os_str().is_empty() {
        return None;
    }
    run_update(id)
}

fn update(
    payload: &Value,
    config: &Config,
    id: &str,
    command: Option<&str>,
    now: u64,
    report: &mut impl FnMut(&dyn std::error::Error),
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
    let mut local_read = false;
    if event == "PreToolUse" {
        // An admission expires at the next pre event, even if its post was lost.
        for read in status.reads.values_mut() {
            read.pending = false;
        }
        if let Some(command) = command.filter(|s| poll::read_only(s)) {
            let key = poll::fingerprint(command, payload["cwd"].as_str().unwrap_or(""));
            let known = status.polls.contains(&key);
            let read = status.reads.entry(key.clone()).or_insert_with(|| {
                storage::read_scope(command, payload["cwd"].as_str().unwrap_or(""))
            });
            // Older status documents predate remote comparison metadata.
            read.remote =
                storage::read_scope(command, payload["cwd"].as_str().unwrap_or("")).remote;
            local_read = !read.remote;
            let comparison_due = read.remote
                && read
                    .compared_at
                    .is_none_or(|last| now.saturating_sub(last) >= 60);
            denied = known && !read.retry && !comparison_due;
            if !denied {
                read.retry = false;
                read.pending = true;
            }
            if !known {
                status.polls.push_back(key);
            }
        }
    }
    if event == "PostToolUse" {
        let success = storage::successful(payload);
        let changed = if success {
            storage::changed_paths(payload, command)
        } else {
            vec![]
        };
        for read in status.reads.values_mut() {
            let local_retry =
                success && !read.remote && command.is_some_and(|s| !poll::read_only(s));
            if local_retry || read.affected_by(&changed) {
                read.retry = true;
            }
        }
        if let Some(command) = command.filter(|s| poll::read_only(s)) {
            let key = poll::fingerprint(command, payload["cwd"].as_str().unwrap_or(""));
            if let Some(read) = status.reads.get_mut(&key) {
                // A post result may only advance a read admitted by its pre hook.
                if read.pending {
                    read.pending = false;
                    if !success {
                        read.retry = true;
                    } else if let Some(output) = storage::output_key(&payload["tool_response"]) {
                        let changed = read.output.as_ref().is_some_and(|old| old != &output);
                        read.retry = changed;
                        if read.remote {
                            read.compared_at = (!changed && read.output.is_some()).then_some(now);
                        }
                        read.output = Some(output);
                    } else if read.remote {
                        // Missing output is not evidence of an identical snapshot.
                        read.compared_at = None;
                    }
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
    // This invocation returns its denial even if persistence fails. Later calls
    // can only consult the previously persisted history, not this failed update.
    let written = storage::atomic_write(&path, &serde_json::to_vec(&document)?);
    if let Err(error) = written {
        report(&error);
    }
    Ok(denied.then(|| {
        if local_read {
            poll::read_refusal()
        } else {
            poll::refusal()
        }
    }))
}
