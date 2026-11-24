//! Live writer lanes and the keeper items each one holds.
//!
//! One reader serves the admission guard and the claim heartbeat: a lane is a native writer
//! run, a Claude writer session or an open pull request, and its items are the `Item key:`,
//! `Plan-Item:` and `### <key> | <title>` lines of its brief, trailers or description.
use crate::run::record::releases_ownership;
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LaneKind {
    Native,
    Claude,
    PullRequest,
}

impl LaneKind {
    pub fn label(self) -> &'static str {
        match self {
            Self::Native => "native run",
            Self::Claude => "Claude writer",
            Self::PullRequest => "open pull request",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Lane {
    pub name: String,
    pub kind: LaneKind,
    /// The run id, session name or pull request number that identifies the holder.
    pub detail: String,
    pub keys: BTreeSet<String>,
}

impl Lane {
    pub fn describe(&self) -> String {
        format!("{} {} ({})", self.kind.label(), self.name, self.detail)
    }
}

pub fn valid_key(key: &str) -> bool {
    key.len() <= 128
        && key.split_once(':').is_some_and(|(project, name)| {
            !project.is_empty() && !name.is_empty() && !name.contains(':')
        })
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

/// The item keys a brief, trailer block or pull request description names.
pub fn item_keys(text: &str) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    for line in text.lines() {
        let line = line.trim();
        let heading = line
            .strip_prefix('#')
            .map(|rest| rest.trim_start_matches('#').split('|').next().unwrap_or(""));
        let named = ["Item key:", "Plan-Item:"]
            .iter()
            .find_map(|prefix| line.strip_prefix(prefix));
        if let Some(candidate) = heading.or(named) {
            let candidate = candidate.trim();
            if valid_key(candidate) {
                keys.insert(candidate.to_owned());
            }
        }
    }
    keys
}

/// The `NAME:` value of a writer brief's Run values, when it has one.
pub fn brief_name(text: &str) -> Option<String> {
    let name = text
        .lines()
        .find_map(|line| line.trim().strip_prefix("NAME:"))?
        .trim();
    (!name.is_empty() && !name.contains(char::is_whitespace)).then(|| name.to_owned())
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_str(&fs::read_to_string(path).ok()?).ok()
}

fn entries(dir: &Path) -> Vec<std::path::PathBuf> {
    let mut paths: Vec<_> = fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .collect();
    paths.sort();
    paths
}

/// Native writer runs whose record still owns its worktree (queued, running or waiting).
pub fn native_lanes(state: &Path) -> Vec<Lane> {
    let mut lanes = Vec::new();
    for dir in entries(&state.join("runs")) {
        let Some(record) = read_json(&dir.join("record.json")) else {
            continue;
        };
        let status = record["status"].as_str().unwrap_or("");
        if record["run_kind"] != "native-codex" || status.is_empty() || releases_ownership(status) {
            continue;
        }
        let run_id = record["run_id"].as_str().unwrap_or("").to_owned();
        let request = read_json(&dir.join("native-request.json")).unwrap_or(Value::Null);
        let prompt = request["prompt"].as_str().unwrap_or("");
        let mut keys = item_keys(prompt);
        for key in [&record["item_key"], &request["card"]["item_key"]] {
            keys.extend(key.as_str().filter(|key| valid_key(key)).map(str::to_owned));
        }
        if keys.is_empty() {
            continue;
        }
        lanes.push(Lane {
            name: brief_name(prompt).unwrap_or_else(|| run_id.clone()),
            kind: LaneKind::Native,
            detail: run_id,
            keys,
        });
    }
    lanes
}

/// A launch record older than this is a session that ended without being marked stopped.
pub const CLAUDE_LANE_MAX_AGE_S: u64 = 24 * 3600;

/// Claude writer sessions the fleet launcher recorded and has not stopped.
pub fn claude_lanes(sessions: &Path, now: u64) -> Vec<Lane> {
    let mut lanes = Vec::new();
    for path in entries(sessions) {
        let file = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if !file.ends_with(".json") || file.ends_with(".ready.json") {
            continue;
        }
        let Some(record) = read_json(&path) else {
            continue;
        };
        let state = record["state"].as_str().unwrap_or("running");
        if !record["stopped_ts"].is_null() || !["launching", "running"].contains(&state) {
            continue;
        }
        let session = record["session_name"].as_str().or(record["name"].as_str());
        let (Some(session), Some(prompt)) = (session, record["prompt_file"].as_str()) else {
            continue;
        };
        if record["ts"]
            .as_u64()
            .is_some_and(|ts| now.saturating_sub(ts) > CLAUDE_LANE_MAX_AGE_S)
        {
            continue;
        }
        let text = fs::read_to_string(prompt).unwrap_or_default();
        let keys = item_keys(&text);
        if keys.is_empty() {
            continue;
        }
        lanes.push(Lane {
            name: brief_name(&text).unwrap_or_else(|| session.to_owned()),
            kind: LaneKind::Claude,
            detail: session.to_owned(),
            keys,
        });
    }
    lanes
}

/// Open pull requests from `gh pr list --json number,headRefName,title,body`.
pub fn pull_request_lanes(json: &str) -> Vec<Lane> {
    let rows: Vec<Value> = serde_json::from_str(json).unwrap_or_default();
    rows.iter()
        .filter_map(|row| {
            let text = format!(
                "{}\n{}",
                row["title"].as_str().unwrap_or(""),
                row["body"].as_str().unwrap_or("")
            );
            let keys = item_keys(&text);
            (!keys.is_empty()).then(|| Lane {
                name: row["headRefName"].as_str().unwrap_or("").to_owned(),
                kind: LaneKind::PullRequest,
                detail: format!("#{}", row["number"]),
                keys,
            })
        })
        .collect()
}

/// Every live lane under `state` (native runs) and `sessions` (Claude launch records).
pub fn local_lanes(state: &Path, sessions: &Path, now: u64) -> Vec<Lane> {
    let mut lanes = native_lanes(state);
    lanes.extend(claude_lanes(sessions, now));
    lanes
}

#[cfg(test)]
#[path = "lane_roster_tests.rs"]
mod tests;
