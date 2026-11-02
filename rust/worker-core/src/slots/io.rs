//! Slot table file IO: `<state>/slots.json` under `<state>/slots.lock`.
//!
//! The table file is never locked itself: every read and rewrite happens while
//! the sibling `slots.lock` is held ([`crate::lockfile`]), and the rewrite is
//! an atomic replace — write a temp file in the same directory, fsync it, then
//! rename it over `slots.json` (`std::fs::rename` is `MoveFileEx` with
//! `MOVEFILE_REPLACE_EXISTING` on Windows). A crash between the temp write and
//! the rename leaves the old `slots.json` intact.

use super::SlotTable;
use crate::lockfile::{self, LockError};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// File plumbing for the slot table: io, parse, lock and version problems.
#[derive(Debug, thiserror::Error)]
pub enum SlotError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("parse: {0}")]
    Parse(#[from] serde_json::Error),
    #[error("lock: {0}")]
    Lock(#[from] LockError),
    #[error("unsupported slots.json version {0}")]
    Version(u32),
}

/// Load the table under the lock, run `f` on it, save it back if it changed.
///
/// A missing `slots.json` is an empty table. The save is skipped entirely when
/// the closure left the table untouched, so a read-only visit never rewrites
/// the file. `deadline` bounds the lock acquisition ([`lockfile::lock_with_retry`]).
pub fn with_table<R>(
    state_dir: &Path,
    deadline: Duration,
    f: impl FnOnce(&mut SlotTable) -> R,
) -> Result<R, SlotError> {
    let mut rng = rand::rng();
    let _guard = lockfile::lock_with_retry(&lock_path(state_dir), deadline, &mut rng)?;
    let path = table_path(state_dir);
    let before = load(&path)?;
    let mut table = before.clone();
    let out = f(&mut table);
    if table != before {
        save(state_dir, &table)?;
    }
    Ok(out)
}

/// Read and parse the table; a missing file is [`SlotTable::default`].
pub(super) fn load(path: &Path) -> Result<SlotTable, SlotError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(SlotTable::default()),
        Err(e) => return Err(e.into()),
    };
    let table: SlotTable = serde_json::from_str(&text)?;
    if table.version != 1 {
        return Err(SlotError::Version(table.version));
    }
    Ok(table)
}

/// Write the table to the temp file and fsync it; the caller renames it.
pub(super) fn write_temp(state_dir: &Path, table: &SlotTable) -> Result<PathBuf, SlotError> {
    let tmp = temp_path(state_dir);
    let json = serde_json::to_vec_pretty(table)?;
    let mut file = std::fs::File::create(&tmp)?;
    std::io::Write::write_all(&mut file, &json)?;
    file.sync_all()?;
    Ok(tmp)
}

/// Atomic replace: temp write (fsynced) then rename over `slots.json`.
pub(super) fn save(state_dir: &Path, table: &SlotTable) -> Result<(), SlotError> {
    let tmp = write_temp(state_dir, table)?;
    std::fs::rename(&tmp, table_path(state_dir))?;
    Ok(())
}

pub(super) fn table_path(state_dir: &Path) -> PathBuf {
    state_dir.join("slots.json")
}

fn lock_path(state_dir: &Path) -> PathBuf {
    state_dir.join("slots.lock")
}

pub(super) fn temp_path(state_dir: &Path) -> PathBuf {
    state_dir.join("slots.json.tmp")
}
