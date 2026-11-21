//! Collect role-scoped observations for shadow digests.
mod github;
mod journal;
mod local;
mod sources;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

const SOURCES: &[&str] = &[
    "keeper",
    "messages",
    "items",
    "ready",
    "hosts",
    "runs",
    "inbox",
    "transcript",
    "decisions",
    "github",
    "main",
    "quota",
];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    role: String,
    principal: String,
    directory: PathBuf,
    inbox: PathBuf,
    transcript: PathBuf,
    session_id: String,
    data_repo: PathBuf,
    decision_base: String,
    github_repo: String,
    github_since: String,
    #[serde(default)]
    pull_requests: Vec<u64>,
    #[serde(default)]
    item_keys: Vec<String>,
    quota: PathBuf,
    #[serde(default = "lease_margin")]
    lease_margin_seconds: i64,
}

fn lease_margin() -> i64 {
    600
}

#[derive(Clone, Default, Deserialize, Serialize)]
struct Bookmark {
    cursor: Value,
    failures: u32,
}

#[derive(Serialize, Deserialize, Default)]
struct Checkpoint {
    scan: u64,
    bytes: u64,
    bookmarks: BTreeMap<String, Bookmark>,
}

#[derive(Deserialize, Serialize)]
struct Event {
    source: String,
    kind: String,
    urgent: bool,
    untrusted: bool,
    data: Value,
}

impl Event {
    fn new(source: &str, kind: &str, data: Value) -> Self {
        Self {
            source: source.into(),
            kind: kind.into(),
            urgent: false,
            untrusted: false,
            data,
        }
    }
}

type Env = BTreeMap<String, String>;
type Observation = (Value, Vec<Event>, bool);
type Result<T> = std::result::Result<T, String>;

type SourceReads = BTreeMap<&'static str, mpsc::Receiver<Result<Observation>>>;

fn scan(
    config: &Arc<Config>,
    keeper: &Arc<crate::keeper_events::Client>,
    state: &mut Checkpoint,
    directory: &std::path::Path,
    pending: &mut SourceReads,
) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(20);
    for &source in SOURCES {
        if let std::collections::btree_map::Entry::Vacant(entry) = pending.entry(source) {
            let cursor = state
                .bookmarks
                .get(source)
                .cloned()
                .unwrap_or_default()
                .cursor;
            let (sender, receiver) = mpsc::channel();
            let config = Arc::clone(config);
            let keeper = Arc::clone(keeper);
            entry.insert(receiver);
            std::thread::spawn(move || {
                let _ = sender.send(sources::read(source, &config, &keeper, cursor));
            });
        }
    }
    let mut next = Checkpoint {
        scan: state.scan.checked_add(1).ok_or("scan number exhausted")?,
        bytes: state.bytes,
        bookmarks: state.bookmarks.clone(),
    };
    let mut events = Vec::new();
    for &source in SOURCES {
        let reading =
            pending[source].recv_timeout(deadline.saturating_duration_since(Instant::now()));
        let reading = match reading {
            Ok(result) => {
                pending.remove(source);
                result
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                pending.remove(source);
                Err("source stopped".into())
            }
            // Keep at most one outstanding read per source. A late result
            // still belongs to the same bookmark, which has not advanced.
            Err(mpsc::RecvTimeoutError::Timeout) => Err("source timeout".into()),
        };
        let bookmark = next.bookmarks.entry(source.into()).or_default();
        let failed = match reading {
            Ok((cursor, rows, failed)) => {
                bookmark.cursor = cursor;
                events.extend(rows);
                failed
            }
            Err(_) => true,
        };
        bookmark.failures = if failed {
            bookmark.failures.saturating_add(1)
        } else {
            0
        };
        if bookmark.failures == 3 {
            let mut event = Event::new(source, "source_unavailable", json!({"failed_scans":3}));
            event.urgent = true;
            events.push(event);
        }
    }
    journal::commit(directory, state.bytes, &mut next, &events)
        .map_err(|error| format!("shadow journal: {error}"))?;
    *state = next;
    Ok(())
}

pub fn run(home: &std::path::Path, env: &Env, args: &[String]) -> Result<()> {
    let [wait, role_flag, role, rest @ ..] = args else {
        return Err("usage: plangraph state wait --role ROLE [--once]".into());
    };
    if wait != "wait"
        || role_flag != "--role"
        || !pg_worker_core::turn_budget::safe_name(role)
        || !(rest.is_empty() || rest == ["--once"])
    {
        return Err("usage: plangraph state wait --role ROLE [--once]".into());
    }
    let config_path = env
        .get("PLANGRAPH_STATE_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            home.join(".config/plangraph/state-shadow")
                .join(format!("{role}.json"))
        });
    let config: Config = serde_json::from_slice(
        &std::fs::read(&config_path).map_err(|_| "shadow configuration unavailable")?,
    )
    .map_err(|_| "invalid shadow configuration")?;
    if config.role != *role
        || config.principal.is_empty()
        || !pg_worker_core::turn_budget::safe_name(&config.session_id)
        || config.transcript.file_stem().and_then(|stem| stem.to_str()) != Some(&config.session_id)
        || config.lease_margin_seconds <= 0
        || config.lease_margin_seconds > 86400
        || !github::valid_repo(&config.github_repo)
        || chrono::DateTime::parse_from_rfc3339(&config.github_since).is_err()
        || !local::sha(&config.decision_base)
    {
        return Err("shadow role or source configuration invalid".into());
    }
    let keeper = Arc::new(crate::keeper_events::Client::new(env)?);
    sources::authorize(&config, &keeper)?;
    let directory = config.directory.join(role);
    pg_worker_core::run::record_io::ensure_private_dir(&directory)
        .map_err(|_| "cannot create role shadow directory")?;
    let _lock = crate::atomic::lock(&directory.join("bookmark.json"))
        .map_err(|_| "shadow collector already active or directory unavailable")?;
    let mut state =
        journal::load(&directory).map_err(|error| format!("shadow journal: {error}"))?;
    let config = Arc::new(config);
    let mut pending = SourceReads::new();
    loop {
        let started = Instant::now();
        scan(&config, &keeper, &mut state, &directory, &mut pending)?;
        if rest == ["--once"] {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(60).saturating_sub(started.elapsed()));
    }
}

#[cfg(test)]
fn fixture(root: &std::path::Path) -> Config {
    serde_json::from_value(json!({
        "role":"routing", "principal":"routing-reader", "directory":root,
        "inbox":root.join("inbox"), "transcript":root.join("session.jsonl"),
        "session_id":"session", "data_repo":root, "decision_base":"a".repeat(40),
        "github_repo":"example/demo", "github_since":"2026-01-01T00:00:00Z",
        "quota":root.join("quota.json"), "item_keys":["demo:A"],
        "lease_margin_seconds":600
    }))
    .unwrap()
}
