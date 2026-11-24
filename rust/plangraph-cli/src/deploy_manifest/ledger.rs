//! The ledger of how long each entry has been not live on this host.
//!
//! `deploy-status` rewrites it on every run: an entry that is live leaves it, an entry that is not live
//! keeps the time it was first seen not live, which starts at the time its change reached the checkout
//! when that is known (so work merged a month ago does not look new). The GO check reads it.
use super::live::Entry;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ledger {
    /// When this ledger was written, epoch seconds.
    pub written_at: i64,
    /// Entry key -> when it was first not live, epoch seconds.
    #[serde(default)]
    pub not_live_since: BTreeMap<String, i64>,
    /// Entry key -> its state text, for the reader of the file.
    #[serde(default)]
    pub state: BTreeMap<String, String>,
    /// SHA-256 of the `deploy/manifest.toml` of the checkout this ledger was computed from.
    #[serde(default)]
    pub manifest: Option<String>,
    /// The release stamp (Rust tree and embedded inputs) of that checkout. The GO check compares both with
    /// the checkout it runs from, so a host on older inputs cannot publish a clean report for them.
    #[serde(default)]
    pub stamp: Option<String>,
}

/// The ledger after a status run: entries not live keep their first-seen time.
pub fn update(prev: &Ledger, entries: &[Entry], now: i64) -> Ledger {
    let mut next = Ledger {
        written_at: now,
        ..Ledger::default()
    };
    for entry in entries.iter().filter(|e| !e.live.is_live()) {
        let key = entry.key();
        let since = prev
            .not_live_since
            .get(&key)
            .copied()
            .or(entry.merged_at)
            .unwrap_or(now)
            .min(now);
        next.not_live_since.insert(key.clone(), since);
        next.state.insert(key, entry.live.text());
    }
    next
}

/// Hours since `since`, rounded down.
pub fn age_hours(since: i64, now: i64) -> i64 {
    (now - since).max(0) / 3600
}

pub fn load(path: &Path) -> Ledger {
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

pub fn save(path: &Path, ledger: &Ledger) -> Result<(), String> {
    let parent = path.parent().ok_or("ledger path has no parent")?;
    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let mut staged = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    serde_json::to_writer_pretty(&mut staged, ledger).map_err(|e| e.to_string())?;
    staged.persist(path).map_err(|e| e.error.to_string())?;
    Ok(())
}

/// A state is `Unreadable` when systemd could not be read; the ledger still counts it, so an
/// unreadable host never reports zero.
pub fn counts(entries: &[Entry]) -> usize {
    entries.iter().filter(|e| !e.live.is_live()).count()
}
