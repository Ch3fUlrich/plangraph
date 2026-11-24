//! Reservations of plan-item keys between two launches that have passed admission but have
//! no run record yet: the admission check and the reservation happen under one lock of the
//! state directory, so two concurrent launches of a key cannot both pass. A launch releases
//! its reservation when it ends or when its run record takes over; the TTL only covers a
//! launcher that died in between.

use crate::lane_guard::Lane;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// A reservation older than this is a launch that never produced a run record.
pub const TTL_SECS: u64 = 120;

fn dir(state: &Path) -> PathBuf {
    state.join("lane-reservations")
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

fn path(state: &Path, key: &str) -> PathBuf {
    let digest = Sha256::digest(key.as_bytes());
    let name: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
    dir(state).join(format!("{name}.json"))
}

/// The unexpired reservations of `keys` held by an owner other than `owner`.
pub fn lanes(state: &Path, keys: &[String], owner: &str, at: u64) -> Vec<Lane> {
    keys.iter()
        .filter_map(|key| {
            let record: Value = serde_json::from_slice(&fs::read(path(state, key)).ok()?).ok()?;
            let held = record["owner"].as_str().filter(|held| *held != owner)?;
            (record["key"] == *key && record["ts"].as_u64()? + TTL_SECS > at).then(|| Lane {
                name: format!("reservation {held}"),
                keys: vec![key.clone()],
                source: "reservation",
            })
        })
        .collect()
}

/// Record `owner` as the holder of `keys`; a later launch of a key finds the reservation.
pub fn reserve(state: &Path, keys: &[String], owner: &str) -> std::io::Result<()> {
    fs::create_dir_all(dir(state))?;
    for key in keys {
        let target = path(state, key);
        let tmp = target.with_extension(format!("{}.tmp", std::process::id()));
        fs::write(
            &tmp,
            json!({"key":key,"owner":owner,"ts":now()}).to_string(),
        )?;
        fs::rename(&tmp, &target)?;
    }
    Ok(())
}

/// Remove the reservations of `keys` that `owner` holds; another owner's are left alone. The
/// caller holds the state's lock (`lock`), so no reservation changes between read and remove.
pub fn release(state: &Path, keys: &[String], owner: &str) {
    for key in keys {
        let target = path(state, key);
        let held = fs::read(&target)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .is_some_and(|record| record["owner"] == owner);
        if held {
            let _ = fs::remove_file(&target);
        }
    }
}

/// Hold the state's admission lock until the guard drops; waits up to ten seconds.
pub fn lock(state: &Path) -> Result<pg_worker_core::lockfile::LockGuard, String> {
    fs::create_dir_all(dir(state)).map_err(|e| format!("lane reservation: {e}"))?;
    let anchor = dir(state).join(".lock");
    let end = Instant::now() + Duration::from_secs(10);
    loop {
        match pg_worker_core::lockfile::try_lock(&anchor) {
            Ok(Some(guard)) => return Ok(guard),
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(20)),
            Ok(None) => return Err("lane reservation lock busy".into()),
            Err(e) => return Err(format!("lane reservation: {e}")),
        }
    }
}

/// A fresh owner token for a launch that names none.
pub fn new_owner() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    format!("launch-{}-{nanos:x}", std::process::id())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reservation_holds_for_other_owners_until_it_expires() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        reserve(dir.path(), &keys, "launch-a").unwrap();
        let at = now();
        assert!(lanes(dir.path(), &keys, "launch-a", at).is_empty());
        let held = lanes(dir.path(), &keys, "launch-b", at);
        assert_eq!(held[0].name, "reservation launch-a");
        assert_eq!(held[0].source, "reservation");
        assert!(lanes(dir.path(), &keys, "launch-b", at + TTL_SECS + 1).is_empty());
        assert!(lanes(dir.path(), &["demo:OTHER".to_owned()], "launch-b", at).is_empty());
    }

    #[test]
    fn release_removes_only_the_reservations_of_its_owner() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned(), "demo:L".to_owned()];
        reserve(dir.path(), &keys[..1], "launch-a").unwrap();
        reserve(dir.path(), &keys[1..], "launch-b").unwrap();
        release(dir.path(), &keys, "launch-a");
        let at = now();
        assert!(lanes(dir.path(), &keys[..1], "launch-c", at).is_empty());
        assert_eq!(lanes(dir.path(), &keys[1..], "launch-c", at).len(), 1);
    }
}
