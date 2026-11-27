//! Writer sessions the fleet launcher started (hand lanes), seen as feeder runs so a hand lane
//! and a feeder run never overlap: each counts toward its host's writers and holds its items.
//! A session the spawner already lists as a run (the launcher registers one under the same
//! `run_id`) is that run's business; only sessions the spawner does not list are added here.
use super::{Env, Value};
use pg_worker_core::host_command;
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime};

/// `run_id` prefix of a lane run: lets decoding tell a lane from a spawner run.
pub(super) const LANE_RUN_PREFIX: &str = "lane:";
/// A job state file this old no longer proves its session is alive; the Claude roster decides.
const STATE_FRESH: Duration = Duration::from_secs(6 * 3600);
const ROSTER_TIMEOUT: Duration = Duration::from_secs(5);

/// The launcher's session records, `$ROUTING_STATE_DIR/launch/sessions` like the launcher itself.
pub(super) fn sessions_dir(home: &Path, env: &Env) -> PathBuf {
    env.get("ROUTING_STATE_DIR")
        .filter(|d| !d.is_empty())
        .map_or_else(|| home.join(".local/state/routing"), PathBuf::from)
        .join("launch/sessions")
}

/// The Claude job directories, under `$CLAUDE_CONFIG_DIR` when set.
pub(super) fn jobs_dir(home: &Path, env: &Env) -> PathBuf {
    env.get("CLAUDE_CONFIG_DIR")
        .filter(|d| !d.is_empty())
        .map_or_else(|| home.join(".claude"), PathBuf::from)
        .join("jobs")
}

/// What `claude agents --json --all` lists, or `None` when the roster cannot be read.
pub(super) fn claude_roster(env: &Env) -> Option<Vec<Value>> {
    let mut command = Command::new("claude");
    for key in ["PATH", "CLAUDE_CONFIG_DIR"] {
        if let Some(value) = env.get(key) {
            command.env(key, value);
        }
    }
    roster_of(command.args(["agents", "--json", "--all"]))
}

/// The roster `cmd` prints, read through the bounded host-command capture (output goes to a
/// file, so a roster larger than a pipe buffer cannot stall the child).
fn roster_of(cmd: &mut Command) -> Option<Vec<Value>> {
    let out = host_command::output(cmd, ROSTER_TIMEOUT).ok()?;
    serde_json::from_slice(&out).ok()
}

/// The all-sessions roster includes terminal jobs.
fn live_roster_state(state: &str) -> bool {
    !matches!(state, "done" | "stopped" | "failed" | "")
}

/// A whole token that is a plan item key (`plangraph:` and the spawner's id characters), never a
/// prefix of other text.
fn is_item_key(token: &str) -> bool {
    token.strip_prefix("plangraph:").is_some_and(|id| {
        !id.is_empty()
            && id.len() <= 180
            && id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
    })
}

/// The plan item keys a lane card names (the card may be a lane name, a key or both).
pub(super) fn item_keys(card: &str) -> Vec<&str> {
    let mut seen = BTreeSet::new();
    card.split(|c: char| c.is_whitespace() || c == ',')
        .filter(|t| is_item_key(t) && seen.insert(*t))
        .collect()
}

#[cfg(target_os = "linux")]
pub(super) fn pid_alive(pid: u64) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}
#[cfg(not(target_os = "linux"))]
pub(super) fn pid_alive(_: u64) -> bool {
    false
}

/// The plan item keys each launcher record names, by the spawner run id the launcher registered
/// for the session. The spawner's own row holds the whole card as one `item_key` string; these
/// keys replace it.
pub(super) fn lane_cards(sessions: &Path) -> BTreeMap<String, Vec<String>> {
    let Ok(entries) = fs::read_dir(sessions) else {
        return BTreeMap::new();
    };
    entries
        .flatten()
        .filter_map(|e| fs::read_to_string(e.path()).ok())
        .filter_map(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|r| r["name"].as_str() == Some("writer"))
        .filter_map(|r| {
            let id = r["run_id"].as_str().filter(|id| !id.is_empty())?;
            let keys = item_keys(r["card"].as_str()?);
            Some((id.to_owned(), keys.into_iter().map(str::to_owned).collect()))
        })
        .collect()
}

/// The spawner lists a launched session as one run whose `item_key` is the whole card string.
/// Replace it with the keys the launcher record parsed from that card, and mark the row as a
/// lane card so each key resolves on its own (a key that left the ready list never fails a tick).
pub(super) fn mark_lane_cards(data: &mut Value, cards: &BTreeMap<String, Vec<String>>) {
    for row in data["runs"].as_array_mut().into_iter().flatten() {
        let Some(keys) = row["run_id"].as_str().and_then(|id| cards.get(id)) else {
            continue;
        };
        row["item_key"] = json!(keys.first().map_or("", String::as_str));
        row["item_keys"] = json!(keys.iter().skip(1).collect::<Vec<_>>());
        row["lane_card"] = json!(true);
    }
}
pub(super) fn is_fleet_row(row: &Value) -> bool {
    row["run_kind"] == "fleet-session" || row["card"]["run_kind"] == "fleet-session"
}

/// Where the lane evidence comes from; the roster and pid probes are injected for tests.
pub(super) struct Evidence<'a> {
    pub sessions: &'a Path,
    pub jobs: &'a Path,
    pub now: SystemTime,
    /// Run ids the spawner lists already (any status): those sessions are not read again.
    pub listed: &'a BTreeSet<String>,
    pub roster: &'a dyn Fn() -> Option<Vec<Value>>,
    pub pid_alive: &'a dyn Fn(u64) -> bool,
}

/// A fresh working job file, a live roster entry or a live PID is sufficient evidence.
/// The spawner run ID is the same when both sources list the session.
pub(super) fn live_lane_runs(ev: &Evidence) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(ev.sessions) else {
        return vec![];
    };
    let mut roster: Option<Option<Vec<Value>>> = None;
    let mut seen = ev.listed.clone();
    let mut rows = vec![];
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Some(record) = fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        else {
            continue;
        };
        let (Some(name), Some(host)) = (
            record["session_name"].as_str(),
            record["host"].as_str().filter(|h| !h.is_empty()),
        ) else {
            continue;
        };
        let id = record["run_id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .map_or_else(|| format!("{LANE_RUN_PREFIX}{name}"), str::to_owned);
        if record["name"].as_str() != Some("writer") || seen.contains(&id) {
            continue;
        }
        let job = record["job_id"]
            .as_str()
            .filter(|j| !j.is_empty() && !j.contains(['/', '.']));
        let state_file = job.map(|j| ev.jobs.join(j).join("state.json"));
        let state = state_file
            .as_ref()
            .and_then(|f| fs::read_to_string(f).ok())
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|s| s["state"].as_str().map(str::to_owned));
        let fresh = state.as_deref() == Some("working")
            && state_file
                .as_ref()
                .and_then(|f| fs::metadata(f).ok())
                .and_then(|m| m.modified().ok())
                .and_then(|at| ev.now.duration_since(at).ok())
                .is_some_and(|age| age <= STATE_FRESH);
        let live = fresh
            || record["pid"]
                .as_u64()
                .is_some_and(|pid| (ev.pid_alive)(pid))
            || record["session_id"]
                .as_str()
                .filter(|id| !id.is_empty())
                .is_some_and(|sid| {
                    roster
                        .get_or_insert_with(|| (ev.roster)())
                        .as_ref()
                        .is_some_and(|agents| {
                            agents.iter().any(|agent| {
                                agent["sessionId"].as_str() == Some(sid)
                                    && agent["state"].as_str().is_none_or(live_roster_state)
                            })
                        })
                });
        if !live {
            continue;
        }
        let keys = record["card"].as_str().map(item_keys).unwrap_or_default();
        seen.insert(id.clone());
        rows.push(json!({
            "run_id": id, "host": host, "status": "running",
            "role": "writer", "started": record["ts"].as_f64(), "lane_card": true,
            "item_key": keys.first().copied().unwrap_or(""),
            "item_keys": keys.iter().skip(1).collect::<Vec<_>>(),
        }));
    }
    rows.sort_by(|a, b| a["run_id"].as_str().cmp(&b["run_id"].as_str()));
    rows
}

#[cfg(test)]
mod tests;
