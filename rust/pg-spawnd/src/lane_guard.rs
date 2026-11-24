//! Admission guard: one plan-item key has one live writer lane on a host.
//!
//! A key moves through three states on one host: free, reserved (an owner token and a time
//! under the flock of `<state>/lane-reservations/.lock`) and held by a run record (status
//! queued or running, role implement, the key in `item_key`, `item_keys` or the brief). The
//! check and the reservation are one step under that lock, taken from the one state path the
//! caller passes in. The reservation is released when `Launched` drops, so a refused or failed
//! launch leaves no claim; the run record that the launch registers takes over. A deliberate
//! second lane needs an override with a nonblank reason, which is kept in the launch record.

use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

/// A lane that holds item keys: a live spawner run, a reservation or an unmerged remote branch.
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

/// Run records and reservations of the spawner state directory and the remote lane branches
/// of a checkout.
pub struct Observed {
    pub state: PathBuf,
    pub repo: PathBuf,
    /// The launch asking: its own reservations are not lanes of another launch.
    pub owner: String,
    /// The claims on the launch's keys, read once with the launch's one clock reading. The sweep
    /// of the lapsed ones works from the same snapshot.
    pub claims: crate::lane_reservation::Snapshot,
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        self.lanes_between(keys, &|| {})
    }
}

impl Observed {
    /// The claims are read BEFORE the runs, and `between` runs in between. A launch registers its
    /// run and only then releases its claim, so a reader that finds no claim reads the runs after
    /// that registration and finds the run: no order of the two steps lets a key through.
    pub fn lanes_between(&self, keys: &[String], between: &dyn Fn()) -> Vec<Lane> {
        let mut lanes = self.claims.lanes(&self.owner);
        between();
        let runs = scan_runs(&self.state);
        lanes.extend(run_lanes(&runs.records));
        // A run record that cannot be read may hold any key: dispatch refuses until it can.
        lanes.extend(runs.unreadable.iter().map(|name| Lane {
            name: format!("unreadable run record {name}"),
            keys: keys.to_vec(),
            source: "unreadable",
        }));
        lanes.extend(branch_lanes(&self.repo, keys));
        lanes
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

/// The keys one launch asks for: those of its brief, then the normalized extra keys of its card.
pub fn launch_keys(brief: &str, extra: &[String]) -> Vec<String> {
    let mut keys = item_keys(brief);
    for key in extra {
        let key = normalize(key);
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    keys
}

/// The plan-item keys a writer brief names, one fixed statement form per line: `## key`,
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

/// The run records of a state directory: those that are queued or running, and the names of those
/// that exist but cannot be read or are not a JSON object (the guard cannot tell what they hold,
/// so they hold every key). A record that vanished meanwhile is not one of them.
pub struct RunScan {
    pub records: Vec<Value>,
    pub unreadable: Vec<String>,
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
        match fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(record) if record.is_object() => {
                    if record["status"]
                        .as_str()
                        .is_some_and(|status| matches!(status, "queued" | "running"))
                    {
                        scan.records.push(record);
                    }
                }
                _ => scan.unreadable.push(run_name(&path)),
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => scan.unreadable.push(run_name(&path)),
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

/// A live writer run holds its `item_key`, the keys of its brief and the keys a fleet
/// session recorded as `item_keys` (the same field batch cards use). Review and orchestration runs only read an item.
pub fn run_lanes(runs: &[Value]) -> Vec<Lane> {
    runs.iter()
        .filter(|record| record["role"] == "implement")
        .filter_map(|record| {
            let name = record["fleet_session_name"]
                .as_str()
                .or_else(|| record["run_id"].as_str())?;
            let mut keys = BTreeSet::new();
            // Record keys are read by the key grammar itself, never through the heading parser.
            keys.extend(record["item_key"].as_str().and_then(record_key));
            for key in record["item_keys"].as_array().into_iter().flatten() {
                keys.extend(key.as_str().and_then(record_key));
            }
            if let Some(brief) = record["manifest"]["brief"].as_str() {
                keys.extend(item_keys(brief));
            }
            (!keys.is_empty()).then(|| Lane {
                name: name.to_owned(),
                keys: keys.into_iter().collect(),
                source: "run",
            })
        })
        .collect()
}

/// The lowercase tokens of a name, split on the delimiters of branch names and keys.
fn tokens(name: &str) -> Vec<String> {
    name.to_ascii_lowercase()
        .split(['/', '-', '_', '.', ':'])
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .collect()
}

/// A key's slug as tokens: the id after the project, so `demo:ALPHA-X` is `alpha`, `x` and
/// `demo:ITEM:detail` is `item`, `detail`.
fn slug(key: &str) -> Vec<String> {
    tokens(key.split_once(':').map_or(key, |(_, id)| id))
}

fn git_output(repo: &Path, args: &[&str]) -> Option<String> {
    Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .ok()
        .filter(|out| out.status.success())
        .map(|out| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The branch a checkout is on; none when detached or not a repository.
fn head_branch(repo: &Path) -> Option<String> {
    git_output(repo, &["symbolic-ref", "--short", "-q", "HEAD"]).map(|name| name.trim().to_owned())
}

/// Remote branches that carry a key's slug as whole tokens and are not merged into
/// `origin/main`: `lane/fix-alpha-x` holds `demo:ALPHA-X` and `demo:FIX`, never `demo:FIXTURE`.
/// The branch the checkout itself is on is its own lane, never a collision.
pub fn branch_lanes(repo: &Path, keys: &[String]) -> Vec<Lane> {
    let git = |args: &[&str]| git_output(repo, args);
    let Some(refs) = git(&[
        "for-each-ref",
        "--no-merged=origin/main",
        "--format=%(refname:short)",
        "refs/remotes/origin",
    ]) else {
        return Vec::new();
    };
    let own = head_branch(repo);
    refs.lines()
        .filter(|name| !name.ends_with("/HEAD") && *name != "origin/main")
        .filter(|name| own.as_deref() != name.strip_prefix("origin/"))
        .filter_map(|name| {
            // The remote's name is not part of the branch: `origin/lane/x` is the branch `lane/x`.
            let branch = tokens(name.strip_prefix("origin/").unwrap_or(name));
            let held: Vec<String> = keys
                .iter()
                .filter(|key| {
                    let slug = slug(key);
                    !slug.is_empty() && branch.windows(slug.len()).any(|part| part == slug)
                })
                .cloned()
                .collect();
            (!held.is_empty()).then(|| Lane {
                name: name.to_owned(),
                keys: held,
                source: "branch",
            })
        })
        .collect()
}

/// Refuse every key of `keys` another live lane holds, naming the lane. With a nonblank
/// override reason a deliberate second lane passes and the reason is returned for the
/// launch record; a blank reason is refused.
pub fn refuse_held(
    keys: &[String],
    lanes: &[Lane],
    override_reason: Option<&str>,
) -> Result<Option<String>, String> {
    // State the guard cannot read or validate is a refusal of its own, before any override: an
    // override admits a second lane next to a lane it can see, never next to one it cannot.
    let unreadable: Vec<&str> = lanes
        .iter()
        .filter(|lane| {
            lane.source == "unreadable" && keys.iter().any(|key| lane.keys.contains(key))
        })
        .map(|lane| lane.name.as_str())
        .collect();
    if !unreadable.is_empty() {
        return Err(format!(
            "lane_unreadable: {}; the keys cannot be admitted until that state can be read, and an override does not apply",
            unreadable.join("; ")
        ));
    }
    let reason = override_reason.map(str::trim);
    if reason == Some("") {
        return Err("lane_override requires a nonblank reason".into());
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
        return Ok(None);
    }
    match reason {
        Some(reason) => Ok(Some(reason.to_owned())),
        None => Err(format!(
            "lane_collision: {}; a deliberate second lane needs a lane_override reason",
            held.join("; ")
        )),
    }
}

/// The admission check of a rendered or plain brief: its keys, and `extra` keys the launch
/// card names, against the observed lanes. `override_reason` comes from explicit launch
/// metadata only (the card's reason), never from the brief text. Returns the
/// override reason that admitted a deliberate second lane and the keys admitted.
pub fn admit(
    brief: &str,
    extra: &[String],
    source: &dyn LaneSource,
    override_reason: Option<&str>,
) -> Result<(Option<String>, Vec<String>), String> {
    let keys = launch_keys(brief, extra);
    let lanes = if keys.is_empty() {
        Vec::new()
    } else {
        source.lanes(&keys)
    };
    refuse_held(&keys, &lanes, override_reason).map(|reason| (reason, keys))
}

pub use crate::lane_launch::{admit_launch, LaunchRequest, Launched, MAX_KEYS};

#[cfg(test)]
#[path = "lane_guard_branch_tests.rs"]
mod branch_tests;
#[cfg(test)]
#[path = "lane_guard_claim_tests.rs"]
mod claim_tests;
#[cfg(test)]
#[path = "lane_guard_launch_tests.rs"]
mod launch_tests;
#[cfg(test)]
#[path = "lane_guard_tests.rs"]
mod tests;
