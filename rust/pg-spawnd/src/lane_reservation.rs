//! Reservations of plan-item keys between two launches that have passed admission but have
//! no run record yet: the admission check and the reservation happen under one lock of the
//! state directory, so two concurrent launches of a key cannot both pass.
//!
//! Each owner has its own file per key (`<key digest>/<owner digest>.json`), so a second
//! launch that deliberately repeats a key never replaces the first launch's claim. A launch
//! releases its own files when it ends or when its run record takes over. A claim of a live
//! launcher holds for as long as that launcher process runs (up to `MAX_AGE_SECS`, longer
//! than any launch call); the claim of a dead process is abandoned at once.

use crate::lane_guard::Lane;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// No claim outlives this, even when its launcher process id was reused: the launch call of the
/// dispatcher gives up long before (its adapter timeout is 200 s).
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

/// Whether the process that wrote a claim still runs. Without `/proc` the age limit alone decides.
fn launcher_alive(pid: u32) -> bool {
    !cfg!(target_os = "linux") || Path::new(&format!("/proc/{pid}")).exists()
}

/// The claims on `keys` of owners other than `owner` that are still active at `at`.
pub fn lanes(state: &Path, keys: &[String], owner: &str, at: u64) -> Vec<Lane> {
    let mut lanes = Vec::new();
    for key in keys {
        let entries = fs::read_dir(key_dir(state, key)).into_iter().flatten();
        for entry in entries.filter_map(Result::ok) {
            let Some(record) = fs::read(entry.path())
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            else {
                continue;
            };
            let (Some(held), Some(ts), Some(pid)) = (
                record["owner"].as_str().filter(|held| *held != owner),
                record["ts"].as_u64(),
                record["pid"]
                    .as_u64()
                    .and_then(|pid| u32::try_from(pid).ok()),
            ) else {
                continue;
            };
            if record["key"] == *key && ts + MAX_AGE_SECS > at && launcher_alive(pid) {
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
        fs::write(
            &tmp,
            json!({"key":key,"owner":owner,"ts":now(),"pid":std::process::id()}).to_string(),
        )?;
        fs::rename(&tmp, &target)?;
    }
    Ok(())
}

/// Remove the claims of `owner` on `keys`; the claims of other owners stay. The caller holds the
/// state's lock (`lock`).
pub fn release(state: &Path, keys: &[String], owner: &str) {
    for key in keys {
        let _ = fs::remove_file(path(state, key, owner));
        // Only an empty directory goes: another owner's claim keeps it.
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

    fn write_claim(state: &Path, key: &str, owner: &str, ts: u64, pid: u32) {
        let target = path(state, key, owner);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(
            target,
            json!({"key":key,"owner":owner,"ts":ts,"pid":pid}).to_string(),
        )
        .unwrap();
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
    fn a_live_launcher_keeps_its_claim_past_two_minutes_and_a_dead_one_loses_it() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:K".to_owned()];
        let at = now();
        // The queue of the Python placement can wait 120 s: the claim holds through it.
        write_claim(
            dir.path(),
            "demo:K",
            "launch-live",
            at - 300,
            std::process::id(),
        );
        assert_eq!(lanes(dir.path(), &keys, "launch-b", at).len(), 1);
        // The process that wrote a claim is gone: the claim is abandoned at once.
        let gone = std::process::Command::new("true").spawn().map(|mut child| {
            let pid = child.id();
            child.wait().unwrap();
            pid
        });
        if cfg!(target_os = "linux") {
            write_claim(dir.path(), "demo:K", "launch-dead", at, gone.unwrap());
            let names: Vec<_> = lanes(dir.path(), &keys, "launch-b", at)
                .into_iter()
                .map(|lane| lane.name)
                .collect();
            assert_eq!(names, ["reservation launch-live"]);
        }
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
        assert_eq!(claim_files(dir.path()), 0);
        assert!(lanes(dir.path(), &keys, "launch-c", now()).is_empty());
    }
}
