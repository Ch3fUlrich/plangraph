//! The claims on some keys as one snapshot: read once, validated by one rule, judged and swept
//! with one clock reading under the admission lock.

use crate::lane_guard::Lane;
use crate::lane_reservation::{active, digest, key_dir, STARTED};
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
};

/// The one rule of what a claim is: an owner, the key whose directory it sits in, a timestamp, a
/// known phase, and the file name its owner gives it. A claim that is anything else is not read
/// as absent: it is held, because its owner cannot be told apart from a claim that is valid.
pub(crate) fn validate_claim(record: &Value, key: &str, file_name: &str) -> Result<(), String> {
    let owner = record["owner"]
        .as_str()
        .filter(|owner| !owner.is_empty())
        .ok_or("missing or empty owner")?;
    if record["key"].as_str() != Some(key) {
        return Err("missing or mismatched key".into());
    }
    if !record["ts"].is_u64() {
        return Err("missing or invalid timestamp".into());
    }
    let phase = &record["phase"];
    if !(phase.is_null() || *phase == STARTED || *phase == "admitted") {
        return Err("unknown phase".into());
    }
    if file_name != format!("{}.json", digest(owner)) {
        return Err("file name is not the owner's".into());
    }
    Ok(())
}

/// One claim entry as a snapshot found it. A claim that cannot be read is not a free key: the
/// owner may still start next to a replacement once it can be read again, so it counts as held.
enum Found {
    Claim(Value),
    Unreadable(String),
}

struct Entry {
    key: String,
    name: String,
    path: PathBuf,
    found: Found,
}

/// What a sweep could not do. Each case is its own kind, so no caller can mistake one for another
/// or ignore it: the admission is refused and says which.
#[derive(Debug, PartialEq, Eq)]
pub enum SweepError {
    /// A claim on the key cannot be read; the key stays held.
    Unreadable(String),
    /// A lapsed claim cannot be removed; the replacement must not be published next to it.
    Remove(String),
}

impl std::fmt::Display for SweepError {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unreadable(what) => write!(out, "claim unreadable, the key stays held: {what}"),
            Self::Remove(what) => write!(out, "lapsed claim cannot be removed: {what}"),
        }
    }
}

#[cfg(test)]
thread_local! {
    /// Makes every removal of a lapsed claim fail on this thread, so a test can fail the sweep
    /// while the publication of a claim still works.
    pub(crate) static REMOVE_FAILS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// The claims on some keys, read once with the one clock reading of an admission. The scan of the
/// claims and the sweep of the lapsed ones both work from it, so they see the same entries.
pub struct Snapshot {
    at: u64,
    entries: Vec<Entry>,
}

impl Snapshot {
    /// Read every claim of `keys`. A claim or directory that cannot be read is an entry of its
    /// own (held); one that vanishes meanwhile (a concurrent release) is not.
    pub fn take(state: &Path, keys: &[String], at: u64) -> Self {
        let mut entries = Vec::new();
        for key in keys {
            let unreadable = |name: String, path: PathBuf, error: &dyn std::fmt::Display| Entry {
                key: key.clone(),
                found: Found::Unreadable(format!("{name}: {error}")),
                name,
                path,
            };
            let key_path = key_dir(state, key);
            let dir = match fs::read_dir(&key_path) {
                Ok(dir) => dir,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    entries.push(unreadable(digest(key), key_path, &error));
                    continue;
                }
            };
            for entry in dir {
                let entry = match entry {
                    Ok(entry) => entry,
                    Err(error) => {
                        entries.push(unreadable(digest(key), key_path.clone(), &error));
                        continue;
                    }
                };
                // Only published claims count: an unfinished write is a `.tmp` file.
                if entry.path().extension().is_none_or(|ext| ext != "json") {
                    continue;
                }
                let name = entry.file_name().to_string_lossy().into_owned();
                match fs::read(entry.path()) {
                    Ok(bytes) => entries.push(
                        match serde_json::from_slice::<Value>(&bytes)
                            .map_err(|error| format!("malformed claim ({error})"))
                            .and_then(|record| validate_claim(&record, key, &name).map(|()| record))
                        {
                            Ok(record) => Entry {
                                key: key.clone(),
                                name,
                                path: entry.path(),
                                found: Found::Claim(record),
                            },
                            Err(why) => unreadable(name, entry.path(), &why),
                        },
                    ),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => entries.push(unreadable(name, entry.path(), &error)),
                }
            }
        }
        Self { at, entries }
    }

    /// The claims of owners other than `owner` that hold at the snapshot's time, and every claim
    /// that cannot be read.
    pub fn lanes(&self, owner: &str) -> Vec<Lane> {
        self.entries
            .iter()
            .filter_map(|entry| {
                let (name, source) = match &entry.found {
                    Found::Unreadable(_) => {
                        (format!("unreadable claim {}", entry.name), "unreadable")
                    }
                    Found::Claim(record) => {
                        let held = record["owner"].as_str().filter(|held| *held != owner)?;
                        (
                            active(record, self.at).then(|| format!("reservation {held}"))?,
                            "reservation",
                        )
                    }
                };
                Some(Lane {
                    name,
                    keys: vec![entry.key.clone()],
                    source,
                })
            })
            .collect()
    }

    /// Remove the claims that lapsed at the snapshot's time, whoever owns them. An admission calls
    /// it under the lock when it grants a key over such a claim, so the replaced claim is gone for
    /// good: a wall clock that steps back cannot make it hold again and let its launch start next
    /// to the replacement. Nothing is removed when a claim cannot be read.
    pub fn sweep(&self) -> Result<(), SweepError> {
        if let Some(Entry {
            found: Found::Unreadable(what),
            ..
        }) = self
            .entries
            .iter()
            .find(|entry| matches!(entry.found, Found::Unreadable(_)))
        {
            return Err(SweepError::Unreadable(what.clone()));
        }
        for entry in &self.entries {
            let Found::Claim(record) = &entry.found else {
                continue;
            };
            if active(record, self.at) {
                continue;
            }
            #[cfg(test)]
            if REMOVE_FAILS.with(std::cell::Cell::get) {
                return Err(SweepError::Remove(format!("{}: injected", entry.name)));
            }
            match fs::remove_file(&entry.path) {
                Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                    return Err(SweepError::Remove(format!("{}: {error}", entry.name)));
                }
                _ => {}
            }
        }
        Ok(())
    }
}
