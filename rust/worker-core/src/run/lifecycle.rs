//! A private host journal. Each immutable row carries the record publication
//! obligation, and the watermark advances only after the projection is durable.
use super::record::valid_run_id;
use super::record_io::{atomic_write_private, ensure_private_dir};
use crate::lockfile::{lock_with_retry, LockGuard};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

const PAGE_SIZE: u64 = 100;

#[derive(Deserialize, Serialize)]
struct Entry {
    path: PathBuf,
    record: Value,
}

struct Journal<'a> {
    state: &'a Path,
    dir: PathBuf,
    _lock: LockGuard,
}

fn read_optional(path: &Path) -> io::Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn invalid(reason: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, reason)
}

fn durable_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    atomic_write_private(path, bytes)?;
    #[cfg(unix)]
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn valid_relative(path: &Path) -> bool {
    path.file_name().is_some_and(|name| name == "record.json")
        && path
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

impl<'a> Journal<'a> {
    fn open(state: &'a Path) -> io::Result<Self> {
        let dir = state.join("run-lifecycle");
        ensure_private_dir(&dir)?;
        #[cfg(unix)]
        fs::File::open(state)?.sync_all()?;
        let lock = lock_with_retry(&dir.join("lock"), Duration::from_secs(3), &mut rand::rng())
            .map_err(io::Error::other)?;
        Ok(Self {
            state,
            dir,
            _lock: lock,
        })
    }

    fn row_path(&self, seq: u64) -> PathBuf {
        self.dir.join(format!("{seq}.json"))
    }

    fn row(&self, seq: u64) -> io::Result<Option<Entry>> {
        read_optional(&self.row_path(seq))?
            .map(|bytes| serde_json::from_slice(&bytes).map_err(io::Error::other))
            .transpose()
    }

    fn watermark(&self) -> io::Result<u64> {
        read_optional(&self.dir.join("published"))?
            .map(|bytes| {
                std::str::from_utf8(&bytes)
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .filter(|seq| *seq < u64::MAX)
                    .ok_or_else(|| invalid("invalid lifecycle publication watermark"))
            })
            .transpose()
            .map(|seq| seq.unwrap_or(0))
    }

    fn complete(&self, seq: u64, entry: &Entry) -> io::Result<()> {
        if !valid_relative(&entry.path) {
            return Err(invalid("invalid lifecycle record path"));
        }
        let path = self.state.join(&entry.path);
        durable_write(&path, &serde_json::to_vec_pretty(&entry.record)?)?;
        if let Some(id) = entry.record["run_id"]
            .as_str()
            .filter(|id| valid_run_id(id))
        {
            if entry.record["external_owner"].is_object() {
                let active = self.dir.join("external").join(id);
                if entry.record["ended"].is_null() {
                    durable_write(&active, b"")?;
                } else {
                    match fs::remove_file(&active) {
                        Ok(()) => {
                            #[cfg(unix)]
                            fs::File::open(active.parent().unwrap())?.sync_all()?;
                        }
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                        Err(error) => return Err(error),
                    }
                }
            }
        }
        #[cfg(unix)]
        for parent in path.ancestors().skip(2).take_while(|p| *p != self.state) {
            fs::File::open(parent)?.sync_all()?;
        }
        #[cfg(unix)]
        fs::File::open(self.state)?.sync_all()?;
        durable_write(&self.dir.join("published"), seq.to_string().as_bytes())
    }

    fn recover(&self) -> io::Result<u64> {
        let published = self.watermark()?;
        if let Some(entry) = self.row(published + 1)? {
            self.complete(published + 1, &entry)?;
            return Ok(published + 1);
        }
        Ok(published)
    }
}

fn projection(record: &Value) -> Value {
    let phase = match record["status"].as_str() {
        Some("queued") => "queued",
        Some("running") => "started",
        _ => "ended",
    };
    let mut event = json!({"phase":phase});
    for key in [
        "run_id",
        "host",
        "role",
        "item_key",
        "status",
        "started",
        "ended",
        "exit_code",
        "review_of",
    ] {
        if let Some(value) = record
            .get(key)
            .filter(|v| v.is_null() || v.is_string() || v.is_number())
        {
            event[key] = value.clone();
        }
    }
    if event["role"] == "review" || event["role"] == "judge" {
        let attempts: Vec<Value> = record["attempts"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|attempt| {
                json!({
                    "status": attempt["status"].as_str(),
                    "exit_code": attempt["exit_code"].as_i64(),
                })
            })
            .collect();
        event["attempts"] = json!(attempts);
    }
    let review = super::review_signal::fields(record);
    event
        .as_object_mut()
        .unwrap()
        .extend(review.as_object().unwrap().clone());
    event
}

pub fn recover(state: &Path) -> io::Result<()> {
    if state.join("run-lifecycle").exists() {
        Journal::open(state)?.recover()?;
    }
    Ok(())
}

pub fn publish(state: &Path, path: &Path, record: &Value) -> io::Result<()> {
    publish_snapshot(state, path, record, None)
}

/// Apply a recovered outcome only while the published snapshot still matches its evidence.
pub fn publish_recovered(
    state: &Path,
    path: &Path,
    before: &Value,
    after: &Value,
) -> io::Result<()> {
    publish_snapshot(state, path, after, Some(before))
}

fn publish_snapshot(
    state: &Path,
    path: &Path,
    record: &Value,
    expected: Option<&Value>,
) -> io::Result<()> {
    let relative = path
        .strip_prefix(state)
        .map_err(|_| invalid("record outside state root"))?;
    if !valid_relative(relative) || !record["run_id"].as_str().is_some_and(valid_run_id) {
        return Err(invalid("invalid lifecycle record identity"));
    }
    let journal = Journal::open(state)?;
    let published = journal.recover()?;
    let previous =
        read_optional(path)?.and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    if expected.is_some() && previous.as_ref() != expected {
        return Ok(());
    }
    if previous
        .as_ref()
        .is_some_and(|previous| projection(previous) == projection(record))
    {
        return atomic_write_private(path, &serde_json::to_vec_pretty(record)?);
    }
    let entry = Entry {
        path: relative.to_owned(),
        record: record.clone(),
    };
    let seq = published
        .checked_add(1)
        .filter(|seq| *seq < u64::MAX)
        .ok_or_else(|| invalid("lifecycle sequence exhausted"))?;
    durable_write(&journal.row_path(seq), &serde_json::to_vec(&entry)?)?;
    journal.complete(seq, &entry)
}

pub fn page(state: &Path, mut cursor: u64) -> io::Result<Value> {
    if !state.join("run-lifecycle").exists() && cursor == 0 {
        return Ok(json!({"rows": [], "next_cursor": 0, "more": false, "review_projection": 1}));
    }
    let journal = Journal::open(state)?;
    let published = journal.recover()?;
    if cursor > published {
        return Err(invalid("lifecycle cursor is ahead of this host"));
    }
    let end = published.min(cursor.saturating_add(PAGE_SIZE));
    let mut rows = Vec::new();
    while cursor < end {
        cursor += 1;
        let entry = journal
            .row(cursor)?
            .ok_or_else(|| invalid("missing lifecycle entry"))?;
        let mut row = projection(&entry.record);
        row["seq"] = json!(cursor);
        rows.push(row);
    }
    Ok(
        json!({"rows": rows, "next_cursor": cursor, "more": cursor < published, "review_projection": 1}),
    )
}
