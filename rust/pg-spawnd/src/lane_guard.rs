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
    time::{SystemTime, UNIX_EPOCH},
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
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        let mut lanes = run_lanes(&live_runs(&self.state));
        lanes.extend(crate::lane_reservation::lanes(
            &self.state,
            keys,
            &self.owner,
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| d.as_secs()),
        ));
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

/// The plan-item keys a writer brief names: `## key`, `### key |`, `Plan item: key`, `Item key: key`.
/// The key is the token up to the next space or bar; one colon ending it is the colon of a
/// `## key: title` heading, not part of the key.
pub fn item_keys(brief: &str) -> Vec<String> {
    let assignment = regex::Regex::new(
        r"(?m)^(?:## |### |Plan item: |Item key: )([A-Za-z0-9._:-]+)(?:[ \t|]|\r?$)",
    )
    .expect("valid assignment pattern");
    let found: BTreeSet<String> = assignment
        .captures_iter(brief)
        .filter_map(|capture| {
            let token = &capture[1];
            let heading = token.strip_suffix(':').unwrap_or(token);
            record_key(heading)
        })
        .collect();
    found.into_iter().collect()
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

/// Records of the runs that are queued or running.
pub fn live_runs(state: &Path) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(state.join("runs")) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("run-"))
        .map(|entry| entry.path().join("record.json"))
        .collect();
    paths.sort();
    paths
        .iter()
        .filter_map(|path| serde_json::from_slice::<Value>(&fs::read(path).ok()?).ok())
        .filter(|record| {
            record["status"]
                .as_str()
                .is_some_and(|status| matches!(status, "queued" | "running"))
        })
        .collect()
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
    let mut keys = item_keys(brief);
    for key in extra {
        let key = normalize(key);
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    let lanes = if keys.is_empty() {
        Vec::new()
    } else {
        source.lanes(&keys)
    };
    refuse_held(&keys, &lanes, override_reason).map(|reason| (reason, keys))
}

/// What a launch was admitted with: its keys and the reason of a deliberate second lane. While
/// it lives the keys stay reserved for this launch; dropping it releases them, so every refused
/// or failed launch path frees the keys and the run record the launch registered holds them.
#[derive(Debug)]
pub struct Launched {
    pub keys: Vec<String>,
    pub lane_override: Option<String>,
    _reservation: Option<Reservation>,
}

/// The reservation of an admitted launch; dropping it releases the keys of this owner.
#[derive(Debug)]
struct Reservation {
    state: PathBuf,
    keys: Vec<String>,
    owner: String,
}

impl Drop for Reservation {
    fn drop(&mut self) {
        // The claim is this owner's own file: it goes whether or not the lock is free. Only the
        // empty key directories are pruned, and only under the lock.
        crate::lane_reservation::release(&self.state, &self.keys, &self.owner);
        if let Ok(_lock) = crate::lane_reservation::lock(&self.state) {
            crate::lane_reservation::prune(&self.state, &self.keys);
        }
    }
}

/// The most keys one launch holds: the run record keeps a batch of at most this many.
pub const MAX_KEYS: usize = 50;

/// One writer launch to admit.
pub struct LaunchRequest<'a> {
    pub prompt: &'a str,
    /// Keys the launch card names besides the brief's own.
    pub extra: &'a [String],
    /// The one state path of the entry point: lock, reservation and run lookup all use it.
    pub state: &'a Path,
    pub repo: &'a Path,
    /// The override reason of the launch card (`lane_override_reason`), the only override a
    /// dispatch admits: a `lane_override` in the typed prompt is metadata for the renderer.
    pub explicit: Option<&'a str>,
    /// Reserve the admitted keys for this launch; a check-only caller (a dry run) leaves them free.
    pub reserve: bool,
}

/// The one admission of a writer launch: render the prompt, take the override from `explicit`,
/// and check the keys of the brief and of the card against the live runs,
/// the remote lane branches of `repo` and the reservations of other launches. With `reserve`
/// the check and the reservation are one step under the state's lock, so two concurrent launches
/// of a key cannot both be admitted.
pub fn admit_launch(request: &LaunchRequest) -> Result<Launched, String> {
    let rendered = crate::batch_writer_brief::render_input(request.prompt)?;
    let owner = crate::lane_reservation::new_owner();
    let observed = Observed {
        state: request.state.to_owned(),
        repo: request.repo.to_owned(),
        owner: owner.clone(),
    };
    let _lock = request
        .reserve
        .then(|| crate::lane_reservation::lock(request.state))
        .transpose()?;
    let (lane_override, keys) = admit(&rendered.brief, request.extra, &observed, request.explicit)?;
    if keys.len() > MAX_KEYS {
        return Err(format!("a launch holds at most {MAX_KEYS} item keys"));
    }
    let reservation = if request.reserve {
        if let Err(error) = crate::lane_reservation::reserve(request.state, &keys, &owner) {
            // Still under the lock: drop what was written before the failure.
            crate::lane_reservation::release(request.state, &keys, &owner);
            crate::lane_reservation::prune(request.state, &keys);
            return Err(format!("lane reservation: {error}"));
        }
        Some(Reservation {
            state: request.state.to_owned(),
            keys: keys.clone(),
            owner,
        })
    } else {
        None
    };
    Ok(Launched {
        keys,
        lane_override,
        _reservation: reservation,
    })
}

#[cfg(test)]
#[path = "lane_guard_tests.rs"]
mod tests;
