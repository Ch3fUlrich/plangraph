//! Admission guard: one plan-item key has one live writer lane on a host.
//!
//! A key is held on one host iff a run record of role implement that has not released its
//! ownership names it in `item_key`, `item_keys` or (for a record without admitted keys) its
//! brief. A record whose liveness cannot be decided holds every requested key and refuses
//! the launch as `lane_unreadable`. The check does not reserve: two dispatches that race are
//! still not serialized.

use crate::record_file::{read_json, ReadError};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

/// The most keys one launch holds: the run record keeps a batch of at most this many.
pub const MAX_KEYS: usize = 50;

/// A lane that holds item keys: a live spawner run, or state that cannot be read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Lane {
    pub name: String,
    pub keys: Vec<String>,
    pub source: &'static str,
}

/// Where the lanes of a set of keys come from.
pub trait LaneSource {
    fn lanes(&self, keys: &[String]) -> Vec<Lane>;
}

/// The run records of one spawner state directory.
pub struct Observed {
    pub state: PathBuf,
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        let runs = scan_runs(&self.state);
        let mut lanes = run_lanes(&runs.records);
        // A run record that cannot be judged may hold any key: dispatch refuses until it can.
        lanes.extend(runs.unreadable.iter().map(|name| Lane {
            name: format!("unreadable run record {name}"),
            keys: keys.to_vec(),
            source: "unreadable",
        }));
        lanes
    }
}

/// Why an admission refused. `code` is the stable reason of the MCP error.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub code: &'static str,
    pub message: String,
}

impl Refusal {
    fn new(code: &'static str, message: String) -> Self {
        Self { code, message }
    }
}

/// Whether `value` is an item key as run records and cards accept it: a project of
/// alphanumerics and `._-`, a colon, and an id of at most 180 characters of alphanumerics and
/// `._:-` (the id may hold further colons); at most 200 characters in all.
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

/// The key a record or card holds: validated by the key grammar alone, as the spawner stores it.
fn record_key(value: &str) -> Option<String> {
    valid_key(value).then(|| normalize(value))
}

/// The keys one launch asks for. A versioned batch input assigns exactly its typed
/// `items[].key`; any other brief assigns the keys of its statement lines. The normalized keys
/// of the launch card come after them.
pub fn launch_keys(prompt: &str, extra: &[String]) -> Result<Vec<String>, String> {
    let mut keys: Vec<String> = Vec::new();
    match crate::batch_writer_brief::typed_keys(prompt) {
        Some(typed) => {
            for key in typed?.iter().filter_map(|key| record_key(key)) {
                if !keys.contains(&key) {
                    keys.push(key);
                }
            }
        }
        None => keys = item_keys(prompt),
    }
    for key in extra {
        let key = normalize(key);
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    Ok(keys)
}

/// The plan-item keys a plain writer brief names, one fixed statement form per line: `## key`,
/// `### key`, `Plan item: key`, `Item key: key`, each followed by nothing, a space or a bar.
/// The key is the whole token up to that. Only a `##`/`###` heading with title text after the
/// token (`## key: title`) reads its last colon as the separator of the title; a token before a
/// bar (`### key: | title`) or alone on its line keeps it, as run records store it.
pub fn item_keys(brief: &str) -> Vec<String> {
    let found: BTreeSet<String> = brief.lines().filter_map(statement_key).collect();
    found.into_iter().collect()
}

fn statement_key(line: &str) -> Option<String> {
    let (heading, rest) = if let Some(rest) = line.strip_prefix("### ").or(line.strip_prefix("## "))
    {
        (true, rest)
    } else {
        (
            false,
            line.strip_prefix("Plan item: ")
                .or(line.strip_prefix("Item key: "))?,
        )
    };
    let end = rest
        .find(|c: char| !(c.is_ascii_alphanumeric() || "._:-".contains(c)))
        .unwrap_or(rest.len());
    let (token, after) = rest.split_at(end);
    if !(after.is_empty() || after.starts_with([' ', '\t', '|', '\r'])) {
        return None;
    }
    let title = after.trim_start();
    let titled = heading && !title.is_empty() && !title.starts_with('|');
    let key = token.strip_suffix(':').filter(|_| titled).unwrap_or(token);
    record_key(key)
}

/// A key as run records keep it: a repeated project prefix of the id part is dropped, so
/// `demo:demo:ITEM` and `demo:ITEM` are one key. The id part keeps every later colon.
pub fn normalize(key: &str) -> String {
    let Some((project, mut id)) = key.split_once(':') else {
        return key.to_owned();
    };
    let prefix = format!("{project}:");
    while id.len() > prefix.len() && id.starts_with(&prefix) {
        id = &id[prefix.len()..];
    }
    format!("{project}:{id}")
}

/// The run records of a state directory that hold their keys (by directory name), and the names of
/// those whose liveness cannot be decided: they hold every key. A record that vanished
/// meanwhile is neither.
pub struct RunScan {
    pub records: Vec<(String, Value)>,
    pub unreadable: Vec<String>,
}

/// Roles a run record may carry that only read an item, never own it.
const READING_ROLES: [&str; 5] = ["review", "orchestrate", "research", "fix", "judge"];

/// What a record's status and role say about its keys.
#[derive(Debug, PartialEq, Eq)]
enum Liveness {
    /// Not a lane of this host, or ended and released: it holds nothing.
    Free,
    Holds,
    /// The record cannot be judged.
    Unreadable,
}

fn truthy(value: &Value) -> bool {
    !matches!(value, Value::Null | Value::Bool(false))
}

/// A record is judged in this order: a receipt of another host's run first, then its status by
/// the shared ownership rule (`run_finished`: ended and no surviving process), then, for a
/// record that still owns its keys, its role. A native writer that was stopped keeps its keys.
fn liveness(record: &Value) -> Liveness {
    if !record.is_object() {
        return Liveness::Unreadable;
    }
    if truthy(&record["overflow_remote"]) || truthy(&record["origin_receipt"]) {
        return Liveness::Free;
    }
    let owns = if record["status"] == "stopped" {
        true
    } else {
        match pg_worker_core::run_finished(record) {
            Ok(finished) => !finished,
            Err(_) => return Liveness::Unreadable,
        }
    };
    if !owns {
        return Liveness::Free;
    }
    match record["role"].as_str() {
        Some("implement") => Liveness::Holds,
        Some(role) if READING_ROLES.contains(&role) => Liveness::Free,
        _ => Liveness::Unreadable,
    }
}

pub fn scan_runs(state: &Path) -> RunScan {
    let mut scan = RunScan {
        records: Vec::new(),
        unreadable: Vec::new(),
    };
    let entries = match fs::read_dir(state.join("runs")) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return scan,
        Err(_) => {
            scan.unreadable.push("runs directory".into());
            return scan;
        }
    };
    let mut paths: Vec<PathBuf> = Vec::new();
    for entry in entries {
        match entry {
            Ok(entry) if entry.file_name().to_string_lossy().starts_with("run-") => {
                paths.push(entry.path().join("record.json"));
            }
            Ok(_) => {}
            Err(_) => scan.unreadable.push("runs directory entry".into()),
        }
    }
    paths.sort();
    for path in paths {
        match read_json(&path) {
            Ok(record) => match liveness(&record) {
                Liveness::Holds => scan.records.push((run_name(&path), record)),
                Liveness::Free => {}
                Liveness::Unreadable => scan.unreadable.push(run_name(&path)),
            },
            Err(ReadError::Missing) => {}
            Err(ReadError::Invalid) => scan.unreadable.push(run_name(&path)),
        }
    }
    scan
}

fn run_name(record: &Path) -> String {
    record
        .parent()
        .and_then(Path::file_name)
        .map_or_else(String::new, |name| name.to_string_lossy().into_owned())
}

/// The keys one holding run record names. Its admitted `item_keys` are the whole membership when
/// the record says they were admitted; any other record also holds the keys of its saved brief.
fn record_keys(record: &Value) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    // Record keys are read by the key grammar itself, never through the heading parser.
    keys.extend(record["item_key"].as_str().and_then(record_key));
    for key in record["item_keys"].as_array().into_iter().flatten() {
        keys.extend(key.as_str().and_then(record_key));
    }
    if record["lane_keys_admitted"] != true {
        if let Some(brief) = record["manifest"]["brief"].as_str() {
            keys.extend(item_keys(brief));
        }
    }
    keys
}

/// A live writer run holds its `item_key`, its `item_keys` and the keys of its brief, as
/// `record_keys` says. It is named by its fleet session, else its run id, else its directory.
/// Review and orchestration runs only read an item.
pub fn run_lanes(runs: &[(String, Value)]) -> Vec<Lane> {
    runs.iter()
        .filter(|(_, record)| record["role"] == "implement")
        .filter_map(|(directory, record)| {
            let name = [&record["fleet_session_name"], &record["run_id"]]
                .into_iter()
                .find_map(Value::as_str)
                .unwrap_or(directory);
            let keys = record_keys(record);
            (!keys.is_empty()).then(|| Lane {
                name: name.to_owned(),
                keys: keys.into_iter().collect(),
                source: "run",
            })
        })
        .collect()
}

/// Refuse every key of `keys` another live lane holds, naming the lane. State the guard cannot
/// judge is a refusal of its own and comes first.
pub fn refuse_held(keys: &[String], lanes: &[Lane]) -> Result<(), Refusal> {
    let unreadable: Vec<&str> = lanes
        .iter()
        .filter(|lane| {
            lane.source == "unreadable" && keys.iter().any(|key| lane.keys.contains(key))
        })
        .map(|lane| lane.name.as_str())
        .collect();
    if !unreadable.is_empty() {
        return Err(Refusal::new(
            "lane_unreadable",
            format!(
                "lane_unreadable: {}; the keys cannot be admitted until that state can be read",
                unreadable.join("; ")
            ),
        ));
    }
    let held: Vec<String> = keys
        .iter()
        .filter_map(|key| {
            lanes
                .iter()
                .find(|lane| lane.keys.contains(key))
                .map(|lane| format!("{key} is held by live lane {} ({})", lane.name, lane.source))
        })
        .collect();
    if held.is_empty() {
        return Ok(());
    }
    Err(Refusal::new(
        "lane_collision",
        format!("lane_collision: {}", held.join("; ")),
    ))
}

/// The admission check of a launch: its keys, and `extra` keys the launch card names, against
/// the observed lanes. Returns the keys admitted. A lane the guard cannot judge refuses first;
/// more keys than a run record keeps are refused after the lanes.
pub fn admit(
    prompt: &str,
    extra: &[String],
    source: &dyn LaneSource,
) -> Result<Vec<String>, Refusal> {
    let keys =
        launch_keys(prompt, extra).map_err(|message| Refusal::new("batch_input", message))?;
    let lanes = if keys.is_empty() {
        Vec::new()
    } else {
        source.lanes(&keys)
    };
    refuse_held(&keys, &lanes)?;
    if keys.len() > MAX_KEYS {
        return Err(Refusal::new(
            "lane_too_many_keys",
            format!("lane_too_many_keys: a launch holds at most {MAX_KEYS} item keys"),
        ));
    }
    Ok(keys)
}

pub use crate::lane_launch::{admit_launch, LaunchRequest};

#[cfg(test)]
#[path = "lane_guard_tests.rs"]
mod tests;
