//! The role's quota file: written from the live quota snapshots each scan, read back as a source.
use super::{Env, Result};
use crate::feeder::quota::{live_snapshots, remaining_by_provider};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// A file older than this was not refreshed by a live writer.
pub(super) const MAX_AGE_SECONDS: u64 = 600;
const SNAPSHOT_MAX_AGE_SECONDS: i64 = 10_800;

pub(super) fn document(levels: &BTreeMap<String, f64>) -> Value {
    let rounded: BTreeMap<_, _> = levels
        .iter()
        .map(|(provider, left)| (provider.clone(), left.round() as i64))
        .collect();
    json!({"remaining_percent": rounded})
}

pub(super) fn write(path: &Path, levels: &BTreeMap<String, f64>) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(&document(levels)).map_err(|_| "quota encode")?;
    pg_worker_core::run::record_io::atomic_write_private(path, &bytes)
        .map_err(|_| "cannot write quota status".into())
}

/// Refresh `path` from the live snapshots (agy, codex and every other provider in the store).
pub(super) fn refresh(path: &Path, env: &Env, repo: &Path, now: i64) -> Result<()> {
    let rows = live_snapshots(env, repo)?;
    write(
        path,
        &remaining_by_provider(&rows, now, SNAPSHOT_MAX_AGE_SECONDS),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn levels_are_written_as_whole_percent_per_provider() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("quota.json");
        let levels = BTreeMap::from([("agy".to_owned(), 41.6), ("codex".to_owned(), 7.2)]);
        write(&path, &levels).unwrap();
        let read: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(read, json!({"remaining_percent": {"agy": 42, "codex": 7}}));
    }
}
