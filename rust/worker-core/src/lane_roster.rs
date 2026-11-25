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

/// The key a run record stores: a doubled project prefix (`p:p:ID`) collapses to `p:ID`.
pub fn normalize_key(value: &str) -> String {
    let Some((project, rest)) = value.split_once(':') else {
        return value.to_owned();
    };
    let mut id = rest;
    while let Some(next) = id.strip_prefix(project).and_then(|r| r.strip_prefix(':')) {
        if next.is_empty() {
            break;
        }
        id = next;
    }
    format!("{project}:{id}")
}

/// Every item a card or run record names, resolved as the run record resolves it: the first
/// non-empty of `item_key` and the `item` alias is *the* item (an invalid one means no item, it
/// never falls through to the alias), else `env_item` (`PLANGRAPH_SPAWN_ITEM`); plus every valid
/// `item_keys` entry.
pub fn card_keys(card: &Value, env_item: Option<&str>) -> BTreeSet<String> {
    let given = |value: &&Value| !value.is_null() && value.as_str() != Some("");
    let primary = match [&card["item_key"], &card["item"]].into_iter().find(given) {
        Some(value) => value.as_str().map(str::to_owned),
        None => env_item.filter(|s| !s.is_empty()).map(str::to_owned),
    };
    let listed = card["item_keys"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str);
    primary
        .iter()
        .map(String::as_str)
        .chain(listed)
        .filter(|key| valid_key(key))
        .map(normalize_key)
        .collect()
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

/// The delivered items of a batch pull request body as `tools/merge_batch_land.py` renders it:
/// the `GO-KEYS [a,b]` block when there is one (authoritative: it leaves deferred keys out even
/// when the table lists them), else the Plan-Item cells of the table rows (`| #12 | a, b | ... |`).
fn batch_pull_request_keys(text: &str) -> BTreeSet<String> {
    let split = |cell: &str| -> Vec<String> {
        cell.split(',')
            .map(str::trim)
            .filter(|key| valid_key(key))
            .map(str::to_owned)
            .collect()
    };
    let go_keys = text
        .lines()
        .filter_map(|line| line.split_once("GO-KEYS [").map(|(_, rest)| rest))
        .map(|rest| split(rest.split(']').next().unwrap_or("")))
        .reduce(|mut all, more| {
            all.extend(more);
            all
        });
    if let Some(go_keys) = go_keys {
        return go_keys.into_iter().collect();
    }
    text.lines()
        .map(str::trim)
        .filter(|line| line.starts_with("| #"))
        .flat_map(|line| split(line.split('|').nth(2).unwrap_or("")))
        .collect()
}

/// The item keys a brief, trailer block or pull request description names.
pub fn item_keys(text: &str) -> BTreeSet<String> {
    let mut keys = batch_pull_request_keys(text);
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

/// Every item a native run names: its record (`item_key`, `item_keys`), its request card and
/// the brief it was given, so a record published before its request still holds the whole batch.
fn run_keys(record: &Value, request: &Value) -> BTreeSet<String> {
    let mut keys = item_keys(request["prompt"].as_str().unwrap_or(""));
    keys.extend(card_keys(record, None));
    keys.extend(card_keys(&request["card"], None));
    keys
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
        let keys = run_keys(&record, &request);
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

/// This host's fleet identity: `FLEET_LOCAL_HOST` (the policy name the launcher records in a
/// session's `host`) when set, else the OS hostname.
pub fn local_host() -> String {
    if let Some(name) = std::env::var("FLEET_LOCAL_HOST")
        .ok()
        .filter(|name| !name.is_empty())
    {
        return name;
    }
    #[cfg(unix)]
    {
        let mut name = [0u8; 256];
        // SAFETY: `name` is writable for its full length.
        if unsafe { libc::gethostname(name.as_mut_ptr().cast(), name.len()) } == 0 {
            let end = name.iter().position(|b| *b == 0).unwrap_or(name.len());
            return String::from_utf8_lossy(&name[..end]).into_owned();
        }
        String::new()
    }
    #[cfg(not(unix))]
    {
        std::env::var("COMPUTERNAME").unwrap_or_default()
    }
}

/// The fleet launcher's one host alias: the policy name of a machine whose own name starts
/// with the alias's first label or its first four letters.
pub fn local_alias() -> String {
    ["coding", "vm"].join(".")
}

/// Whether the fleet host `host` is this machine (`local`): the same name, or the launcher's
/// one alias (`local_alias`) on a machine of the matching own name.
pub fn is_local_host(host: &str, local: &str) -> bool {
    let alias = local_alias();
    let label = alias.split('.').next().unwrap_or_default();
    host == local
        || (host == alias
            && local
                .split('.')
                .next()
                .is_some_and(|own| own == label || own == &label[..4]))
}

/// Claude writer sessions the fleet launcher recorded and has not stopped.
///
/// A record joins the fleet lifecycle by its session id: `live(None)` lists the session ids
/// `claude agents` shows as not finished on this host, `live(Some(ssh))` the same on the host a
/// remote record names in `ssh` (the sender mirrors it into the record). An exited session
/// drops out at once and a writer that runs for days stays. A lifecycle that cannot be read
/// (`None`), or a remote record without an `ssh` destination, keeps every unstopped record.
pub fn claude_lanes(
    sessions: &Path,
    live: &dyn Fn(Option<&str>) -> Option<BTreeSet<String>>,
    local_host: &str,
) -> Vec<Lane> {
    let mut lanes = Vec::new();
    let mut polled: std::collections::BTreeMap<Option<String>, Option<BTreeSet<String>>> =
        std::collections::BTreeMap::new();
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
        let remote = record["host"]
            .as_str()
            .is_some_and(|host| !host.is_empty() && !is_local_host(host, local_host));
        let destination = record["ssh"].as_str().filter(|ssh| !ssh.is_empty());
        if !remote || destination.is_some() {
            let target = destination.filter(|_| remote).map(str::to_owned);
            let listed = polled
                .entry(target.clone())
                .or_insert_with(|| live(target.as_deref()));
            if let Some(listed) = listed {
                if !record["session_id"]
                    .as_str()
                    .is_some_and(|id| listed.contains(id))
                {
                    continue;
                }
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
const FINISHED: [&str; 6] = [
    "finished",
    "stopped",
    "done",
    "failed",
    "cancelled",
    "crashed",
];

/// Run `command` (a `claude agents --json`) and return the ids of the sessions not finished;
/// `None` when it cannot be read: no `claude`, a failed or slow poll.
fn poll_sessions(mut command: std::process::Command) -> Option<BTreeSet<String>> {
    let mut child = command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        std::io::Read::read_to_string(&mut stdout, &mut text).map(|_| text)
    });
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
    let text = reader.join().ok()?.ok()?;
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

/// The live Claude sessions of this host, or of the host reached by the ssh destination `ssh`.
pub fn live_claude_sessions_on(ssh: Option<&str>) -> Option<BTreeSet<String>> {
    let command = match ssh {
        None => {
            let mut command = std::process::Command::new("claude");
            command.args(["agents", "--json"]);
            command
        }
        Some(destination) if !destination.is_empty() && !destination.starts_with('-') => {
            let mut command = std::process::Command::new("ssh");
            command
                .args(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", destination])
                .arg("bash -lc 'claude agents --json'");
            command
        }
        Some(_) => return None,
    };
    poll_sessions(command)
}

/// The live Claude sessions of this host (`None`: unreadable).
pub fn live_claude_sessions() -> Option<BTreeSet<String>> {
    live_claude_sessions_on(None)
}

/// Open pull requests from `gh pr list --json number,headRefName,title,body`; a pull request
/// holds the items it names and, by the keeper's branch convention, those its branch carries.
pub fn pull_request_lanes(json: &str) -> Vec<Lane> {
    let rows: Vec<Value> = serde_json::from_str(json).unwrap_or_default();
    rows.iter()
        .map(|row| {
            let text = format!(
                "{}\n{}",
                row["title"].as_str().unwrap_or(""),
                row["body"].as_str().unwrap_or("")
            );
            let branch = row["headRefName"].as_str().unwrap_or("").to_owned();
            Lane {
                name: branch.clone(),
                kind: LaneKind::PullRequest,
                detail: format!("#{}", row["number"]),
                keys: item_keys(&text),
                branch: Some(branch).filter(|b| !b.is_empty()),
            }
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
            let keys: BTreeSet<String> = row["keys"]
                .as_array()?
                .iter()
                .filter_map(|k| k.as_str().filter(|k| valid_key(k)).map(str::to_owned))
                .collect();
            // The reservation yields to the run record only once the record holds every
            // reserved item (or has ended): a record published ahead of its batch must not
            // open a window for a second submit.
            let dir = state.join("runs").join(run_id);
            let published = read_json(&dir.join("record.json")).is_some_and(|record| {
                let request = read_json(&dir.join("native-request.json")).unwrap_or(Value::Null);
                let ended = record["status"].as_str().is_some_and(releases_ownership);
                ended || keys.is_subset(&run_keys(&record, &request))
            });
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

/// Every live local lane: reservations and native runs under `state`, Claude launch records.
pub fn local_lanes(
    state: &Path,
    sessions: &Path,
    live: &dyn Fn(Option<&str>) -> Option<BTreeSet<String>>,
    now: u64,
) -> Vec<Lane> {
    local_lanes_between(state, sessions, live, now, || {})
}

/// `local_lanes` with a hook that runs between the reservation scan and the run scan.
///
/// Reservations are read first: a run record only ever gains items, so a reservation that was
/// dropped because the record was complete is followed by a run scan that sees the complete
/// record, and a reservation kept for an incomplete record is covered by the run scan if the
/// record completed meanwhile. Reading the runs first would let a duplicate through between
/// an incomplete record and the reservation's removal.
pub fn local_lanes_between(
    state: &Path,
    sessions: &Path,
    live: &dyn Fn(Option<&str>) -> Option<BTreeSet<String>>,
    now: u64,
    between: impl FnOnce(),
) -> Vec<Lane> {
    let mut lanes = reserved_lanes(state, now);
    between();
    lanes.extend(native_lanes(state));
    lanes.extend(claude_lanes(sessions, live, &local_host()));
    lanes
}

#[cfg(test)]
#[path = "lane_roster_tests.rs"]
mod tests;
