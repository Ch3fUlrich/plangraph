//! Reservations of plan-item keys between two launches that have passed admission but have
//! no run record yet: the admission check and the reservation happen under one lock of the
//! state directory, so two concurrent launches of a key cannot both pass.
//!
//! Each owner has its own file per key (`<key digest>/<owner digest>.json`), so a second
//! launch that deliberately repeats a key never replaces the first launch's claim. A launch
//! releases its own files when it ends or when its run record takes over. A launch that was
//! cut off (the dispatcher died) is not forgotten: the Python supervisor it started can still
//! register the run, so its claim holds until `MAX_AGE_SECS` has passed, whether or not the
//! dispatcher process is still alive.

use crate::lane_guard::Lane;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// How long a claim holds that nobody released: longer than any launch can take to register its
/// run record (the adapter call of the dispatcher gives up after 200 s), so it only ever frees
/// the key of a launch that died.
pub const MAX_AGE_SECS: u64 = 600;

fn dir(state: &Path) -> PathBuf {
    state.join("lane-reservations")
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

fn digest(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .take(8)
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn key_dir(state: &Path, key: &str) -> PathBuf {
    dir(state).join(digest(key))
}

fn path(state: &Path, key: &str, owner: &str) -> PathBuf {
    key_dir(state, key).join(format!("{}.json", digest(owner)))
}

/// The claims on `keys` of owners other than `owner` that are still active at `at`.
pub fn lanes(state: &Path, keys: &[String], owner: &str, at: u64) -> Vec<Lane> {
    let mut lanes = Vec::new();
    for key in keys {
        let entries = fs::read_dir(key_dir(state, key)).into_iter().flatten();
        // Only published claims count: an unfinished write is a `.tmp` file.
        for entry in entries
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        {
            let Some(record) = fs::read(entry.path())
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            else {
                continue;
            };
            let (Some(held), Some(ts)) = (
                record["owner"].as_str().filter(|held| *held != owner),
                record["ts"].as_u64(),
            ) else {
                continue;
            };
            if record["key"] == *key && ts + MAX_AGE_SECS > at {
                lanes.push(Lane {
                    name: format!("reservation {held}"),
                    keys: vec![key.clone()],
                    source: "reservation",
                });
            }
        }
    }
    lanes
}

/// Record `owner` as a holder of `keys`; a later launch of a key finds the claim.
pub fn reserve(state: &Path, keys: &[String], owner: &str) -> std::io::Result<()> {
    for key in keys {
        fs::create_dir_all(key_dir(state, key))?;
        let target = path(state, key, owner);
        let tmp = target.with_extension(format!("{}.tmp", std::process::id()));
        let published = fs::write(
            &tmp,
            json!({"key":key,"owner":owner,"ts":now()}).to_string(),
        )
        .and_then(|()| fs::rename(&tmp, &target));
        if published.is_err() {
            // No unfinished file is left behind to be mistaken for, or to block, a claim.
            let _ = fs::remove_file(&tmp);
        }
        published?;
    }
    Ok(())
}

/// Remove the claims of `owner` on `keys`; the claims of other owners stay. Each owner has its
/// own files, so this is safe without the lock.
pub fn release(state: &Path, keys: &[String], owner: &str) {
    for key in keys {
        let _ = fs::remove_file(path(state, key, owner));
    }
}

/// Remove the directories of `keys` that no claim keeps. The caller holds the state's lock
/// (`lock`): a reservation creates its directory under it.
pub fn prune(state: &Path, keys: &[String]) {
    for key in keys {
        let _ = fs::remove_dir(key_dir(state, key));
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

/// The number of claim files under the state.
pub fn claim_files(state: &Path) -> usize {
    fs::read_dir(dir(state))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .flat_map(|entry| fs::read_dir(entry.path()).into_iter().flatten())
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        .count()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_claim(state: &Path, key: &str, owner: &str, ts: u64) {
        let target = path(state, key, owner);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(target, json!({"key":key,"owner":owner,"ts":ts}).to_string()).unwrap();
    }

    #[test]
    fn a_reservation_holds_for_other_owners_until_it_is_abandoned() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        reserve(dir.path(), &keys, "launch-a").unwrap();
        let at = now();
        assert!(lanes(dir.path(), &keys, "launch-a", at).is_empty());
        let held = lanes(dir.path(), &keys, "launch-b", at);
        assert_eq!(held[0].name, "reservation launch-a");
        assert_eq!(held[0].source, "reservation");
        assert!(lanes(dir.path(), &keys, "launch-b", at + MAX_AGE_SECS + 1).is_empty());
        assert!(lanes(dir.path(), &["demo:OTHER".to_owned()], "launch-b", at).is_empty());
    }

    #[test]
    fn a_claim_survives_the_exit_of_the_dispatcher_that_wrote_it() {
        // The dispatcher died during admission, but the supervisor it started can still register
        // the run: a restarted dispatcher meets the claim until it is old enough to be abandoned.
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        let at = now();
        write_claim(dir.path(), "demo:K", "launch-from-a-dead-process", at - 300);
        assert_eq!(lanes(dir.path(), &keys, "launch-b", at).len(), 1);
        assert!(lanes(dir.path(), &keys, "launch-b", at + 301).is_empty());
    }

    #[test]
    fn an_unfinished_write_is_not_a_claim_and_a_failed_publication_leaves_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        let stray = key_dir(dir.path(), "demo:K");
        fs::create_dir_all(&stray).unwrap();
        let record = json!({"key":"demo:K","owner":"launch-a","ts":now()}).to_string();
        fs::write(stray.join("0123.4567.tmp"), &record).unwrap();
        assert!(lanes(dir.path(), &keys, "launch-b", now()).is_empty());
        // The rename cannot replace a directory: the temporary file must not outlive the failure.
        fs::create_dir_all(path(dir.path(), "demo:K", "launch-b")).unwrap();
        assert!(reserve(dir.path(), &keys, "launch-b").is_err());
        let own = format!("{}.tmp", std::process::id());
        let leftovers: Vec<_> = fs::read_dir(&stray)
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.ends_with(&own))
            .collect();
        assert!(leftovers.is_empty(), "{leftovers:?}");
    }

    #[test]
    fn a_second_owner_never_replaces_the_first_claim() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        reserve(dir.path(), &keys, "launch-a").unwrap();
        reserve(dir.path(), &keys, "launch-b").unwrap();
        assert_eq!(claim_files(dir.path()), 2);
        release(dir.path(), &keys, "launch-b");
        let held = lanes(dir.path(), &keys, "launch-c", now());
        assert_eq!(held.len(), 1);
        assert_eq!(held[0].name, "reservation launch-a");
        release(dir.path(), &keys, "launch-a");
        prune(dir.path(), &keys);
        assert_eq!(claim_files(dir.path()), 0);
        assert!(!key_dir(dir.path(), "demo:K").exists());
        assert!(lanes(dir.path(), &keys, "launch-c", now()).is_empty());
    }
}
