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
    /// An unmerged branch that carries the item id: the keeper's open lane.
    Branch,
}

impl LaneKind {
    pub fn label(self) -> &'static str {
        match self {
            Self::Native => "native run",
            Self::Claude => "Claude writer",
            Self::PullRequest => "open pull request",
            Self::Branch => "open lane branch",
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
    /// The lane's branch, which holds every item whose id it carries (keeper convention).
    pub branch: Option<String>,
}

impl Lane {
    /// Whether this lane holds `key`: it names the key, or its branch carries the key's id.
    pub fn holds(&self, key: &str) -> bool {
        self.keys.contains(key)
            || self.branch.as_deref().is_some_and(|branch| {
                let id = key.rsplit(':').next().unwrap_or("");
                !id.is_empty() && branch.to_lowercase().contains(&id.to_lowercase())
            })
    }

    pub fn describe(&self) -> String {
        format!("{} {} ({})", self.kind.label(), self.name, self.detail)
    }
}

/// The one item-key shape (the native run record and the lane roster share it).
pub fn valid_key(value: &str) -> bool {
    let Some((project, id)) = value.split_once(':') else {
        return false;
    };
    value.len() <= 200
        && !project.is_empty()
        && !id.is_empty()
        && id.len() <= 180
        && project
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

const BATCH_PREFIX: &str = "PLANGRAPH-BATCH-INPUT-V1";

/// The JSON of a versioned batch input, which the launcher renders into a brief later.
fn batch_payload(text: &str) -> Option<Value> {
    let rest = text.strip_prefix(BATCH_PREFIX)?;
    let rest = rest
        .strip_prefix("\r\n")
        .or_else(|| rest.strip_prefix('\n'))?;
    serde_json::from_str(rest).ok()
}

/// The item keys a brief, trailer block or pull request description names.
pub fn item_keys(text: &str) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    if let Some(input) = batch_payload(text) {
        for item in input["items"].as_array().into_iter().flatten() {
            keys.extend(
                item["key"]
                    .as_str()
                    .filter(|k| valid_key(k))
                    .map(str::to_owned),
            );
        }
        return keys;
    }
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
    if let Some(input) = batch_payload(text) {
        return input["run"]["name"].as_str().map(str::to_owned);
    }
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
        let card = &request["card"];
        let listed = card["item_keys"].as_array().into_iter().flatten();
        for key in [&record["item_key"], &card["item_key"]]
            .into_iter()
            .chain(listed)
        {
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
            branch: None,
        });
    }
    lanes
}

/// Claude writer sessions the fleet launcher recorded and has not stopped.
///
/// `live` is the set of session ids `claude agents` lists as not finished: a record joins the
/// fleet lifecycle by its session id, so an exited session drops out at once and a writer that
/// runs for days stays. When the lifecycle is unknown (`None`) every unstopped record counts.
pub fn claude_lanes(sessions: &Path, live: Option<&BTreeSet<String>>) -> Vec<Lane> {
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
        if record["name"] != "writer"
            || !record["stopped_ts"].is_null()
            || !["launching", "running"].contains(&state)
        {
            continue;
        }
        if let Some(live) = live {
            if !record["session_id"]
                .as_str()
                .is_some_and(|id| live.contains(id))
            {
                continue;
            }
        }
        let session = record["session_name"].as_str().unwrap_or("writer");
        let Some(prompt) = record["prompt_file"].as_str() else {
            continue;
        };
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
            branch: None,
        });
    }
    lanes
}

/// `claude agents` states that mean the session is over (the fleet lifecycle's own list).
const FINISHED: [&str; 4] = ["finished", "stopped", "done", "failed"];

/// Ids of the Claude sessions `claude agents --json` lists as not finished; `None` when the
/// lifecycle cannot be read (no `claude`, a failed or slow poll), so callers keep every record.
pub fn live_claude_sessions() -> Option<BTreeSet<String>> {
    let mut child = std::process::Command::new("claude")
        .args(["agents", "--json"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let started = std::time::Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().ok()? {
            break status;
        }
        if started.elapsed() > std::time::Duration::from_secs(15) {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    };
    let mut text = String::new();
    std::io::Read::read_to_string(&mut child.stdout.take()?, &mut text).ok()?;
    let rows: Vec<Value> = serde_json::from_str(&text)
        .ok()
        .filter(|_| status.success())?;
    Some(
        rows.iter()
            .filter(|row| {
                !row["state"]
                    .as_str()
                    .is_some_and(|state| FINISHED.contains(&state))
            })
            .filter_map(|row| row["sessionId"].as_str().map(str::to_owned))
            .collect(),
    )
}

/// Open pull requests from `gh pr list --json number,headRefName,title,body`; a pull request
/// holds the items it names and, by the keeper's branch convention, those its branch carries.
pub fn pull_request_lanes(json: &str) -> Vec<Lane> {
    let rows: Vec<Value> = serde_json::from_str(json).unwrap_or_default();
    rows.iter()
        .filter_map(|row| {
            let text = format!(
                "{}\n{}",
                row["title"].as_str().unwrap_or(""),
                row["body"].as_str().unwrap_or("")
            );
            let branch = row["headRefName"].as_str().unwrap_or("").to_owned();
            Some(Lane {
                name: branch.clone(),
                kind: LaneKind::PullRequest,
                detail: format!("#{}", row["number"]),
                keys: item_keys(&text),
                branch: Some(branch).filter(|b| !b.is_empty()),
            })
        })
        .collect()
}

/// Unmerged branches (the keeper's open lanes): each holds the items its name carries.
pub fn branch_lanes<'a>(names: impl IntoIterator<Item = &'a str>) -> Vec<Lane> {
    names
        .into_iter()
        .filter(|name| !name.is_empty())
        .map(|name| Lane {
            name: name.to_owned(),
            kind: LaneKind::Branch,
            detail: name.to_owned(),
            keys: BTreeSet::new(),
            branch: Some(name.to_owned()),
        })
        .collect()
}

/// A reservation older than this never became a run record: its submit died.
pub const RESERVATION_TTL_S: u64 = 600;

fn reservation_path(state: &Path, run_id: &str) -> std::path::PathBuf {
    state
        .join("lane-reservations")
        .join(format!("{run_id}.json"))
}

/// Hold `keys` for a run that is about to publish its record, so a concurrent submit sees them.
pub fn write_reservation(
    state: &Path,
    run_id: &str,
    name: &str,
    keys: &BTreeSet<String>,
    now: u64,
) -> std::io::Result<()> {
    if !crate::run::record::valid_run_id(run_id) {
        return Err(std::io::Error::other("invalid run id"));
    }
    let path = reservation_path(state, run_id);
    fs::create_dir_all(path.parent().unwrap())?;
    let body = serde_json::json!({"run_id": run_id, "name": name, "keys": keys, "ts": now});
    crate::run::record_io::atomic_write_private(&path, body.to_string().as_bytes())
}

/// Reservations whose run has not yet published a record.
pub fn reserved_lanes(state: &Path, now: u64) -> Vec<Lane> {
    entries(&state.join("lane-reservations"))
        .iter()
        .filter_map(|path| {
            let row = read_json(path)?;
            let run_id = row["run_id"].as_str()?;
            let fresh = now.saturating_sub(row["ts"].as_u64()?) < RESERVATION_TTL_S;
            let published = state.join("runs").join(run_id).join("record.json").exists();
            let keys: BTreeSet<String> = row["keys"]
                .as_array()?
                .iter()
                .filter_map(|k| k.as_str().filter(|k| valid_key(k)).map(str::to_owned))
                .collect();
            (fresh && !published && !keys.is_empty()).then(|| Lane {
                name: row["name"].as_str().unwrap_or(run_id).to_owned(),
                kind: LaneKind::Native,
                detail: run_id.to_owned(),
                keys,
                branch: None,
            })
        })
        .collect()
}

/// Every live local lane: native runs and reservations under `state`, Claude launch records.
pub fn local_lanes(
    state: &Path,
    sessions: &Path,
    live_sessions: Option<&BTreeSet<String>>,
    now: u64,
) -> Vec<Lane> {
    let mut lanes = native_lanes(state);
    lanes.extend(reserved_lanes(state, now));
    lanes.extend(claude_lanes(sessions, live_sessions));
    lanes
}

#[cfg(test)]
#[path = "lane_roster_tests.rs"]
mod tests;
