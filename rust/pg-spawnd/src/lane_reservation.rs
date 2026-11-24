//! Reservations of plan-item keys between two launches that have passed admission but have
//! no run record yet: the admission check and the reservation happen under one lock of the
//! state directory, so two concurrent launches of a key cannot both pass.
//!
//! Each owner has its own file per key (`<key digest>/<owner digest>.json`), so a second
//! launch that deliberately repeats a key never replaces the first launch's claim. A launch
//! releases its own files when it ends or when its run record takes over, under the state's
//! lock, so an admission that scanned the runs before the registration also sees the claim.
//! A claim has two phases. Admitted (`PENDING_SECS`): no supervisor exists yet, so a dispatcher
//! that died holds the key only briefly. Started (`STARTED_SECS`): the Python supervisor was
//! started and can still register the run after the dispatcher is gone, so the claim holds
//! longer, whether or not the dispatcher process is alive.

use crate::lane_guard::Lane;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(test)]
thread_local! {
    /// Called by `lock_for` on the current thread each time it finds the lock held, so a test
    /// can wait for a release to be blocked instead of guessing with a timeout.
    pub(crate) static BUSY_HOOK: std::cell::RefCell<Option<Box<dyn Fn()>>> =
        const { std::cell::RefCell::new(None) };
}

/// How long an admitted claim holds before its launch starts a supervisor.
pub const PENDING_SECS: u64 = 120;

/// How long a started claim holds that nobody released: longer than any launch can take to
/// register its run record (the adapter call of the dispatcher gives up after 200 s), so it only
/// ever frees the key of a launch that died.
pub const STARTED_SECS: u64 = 600;

const STARTED: &str = "started";

fn dir(state: &Path) -> PathBuf {
    state.join("lane-reservations")
}

#[cfg(test)]
thread_local! {
    /// Scripted readings of the claim clock on this thread: each read takes the first one, the
    /// last one repeats. A test scripts a wall clock that steps back between two reads.
    pub(crate) static CLOCK: std::cell::RefCell<Vec<u64>> =
        const { std::cell::RefCell::new(Vec::new()) };
}

/// The current time in seconds, the clock of every claim. An admission reads it once, under the
/// lock, and decides with that one reading.
pub fn now_secs() -> u64 {
    #[cfg(test)]
    if let Some(scripted) = CLOCK.with(|clock| {
        let mut clock = clock.borrow_mut();
        match clock.len() {
            0 => None,
            1 => Some(clock[0]),
            _ => Some(clock.remove(0)),
        }
    }) {
        return scripted;
    }
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

fn read_claim(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

/// Whether a claim still holds its key at `at`: an admitted claim for `PENDING_SECS`, a started
/// one for `STARTED_SECS`.
fn active(record: &Value, at: u64) -> bool {
    let hold = if record["phase"] == STARTED {
        STARTED_SECS
    } else {
        PENDING_SECS
    };
    record["ts"].as_u64().is_some_and(|ts| ts + hold > at)
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
            let Some(record) = read_claim(&entry.path()) else {
                continue;
            };
            let Some(held) = record["owner"].as_str().filter(|held| *held != owner) else {
                continue;
            };
            if record["key"] == *key && active(&record, at) {
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
pub fn reserve(state: &Path, keys: &[String], owner: &str, at: u64) -> std::io::Result<()> {
    for key in keys {
        fs::create_dir_all(key_dir(state, key))?;
        let target = path(state, key, owner);
        let tmp = target.with_extension(format!("{}.tmp", std::process::id()));
        let published = fs::write(&tmp, json!({"key":key,"owner":owner,"ts":at}).to_string())
            .and_then(|()| fs::rename(&tmp, &target));
        if published.is_err() {
            // No unfinished file is left behind to be mistaken for, or to block, a claim.
            let _ = fs::remove_file(&tmp);
        }
        published?;
    }
    Ok(())
}

/// Mark the claims of `owner` on `keys` as started: the launch is about to start its supervisor,
/// which can register the run after the dispatcher is gone. Under the state's lock, and only
/// while every claim is still this owner's and unexpired: a dispatcher that was paused past the
/// deadline of its claim must not revive it after another admission took the key, so that launch
/// is refused instead.
pub fn start(state: &Path, keys: &[String], owner: &str) -> Result<(), String> {
    let _lock = lock(state)?;
    let at = now_secs();
    for key in keys {
        let held = read_claim(&path(state, key, owner)).is_some_and(|record| {
            record["owner"] == owner && record["key"] == *key && active(&record, at)
        });
        if !held {
            return Err(format!(
                "the claim on {key} expired before the launch started; retry the dispatch"
            ));
        }
    }
    for key in keys {
        let target = path(state, key, owner);
        let tmp = target.with_extension(format!("{}.tmp", std::process::id()));
        let record = json!({"key":key,"owner":owner,"ts":at,"phase":STARTED});
        let published =
            fs::write(&tmp, record.to_string()).and_then(|()| fs::rename(&tmp, &target));
        if published.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        published.map_err(|e| format!("lane reservation: {e}"))?;
    }
    Ok(())
}

/// Remove the claims on `keys` that no longer hold at `at`, whoever owns them. An admission calls
/// it under the lock when it grants a key over such a claim, so the replaced claim is gone for
/// good: a wall clock that steps back cannot make it hold again and let its launch start next to
/// the replacement.
///
/// `at` is the reading the admission scanned with: the sweep judges every claim as the scan did,
/// and a claim that cannot be removed refuses the admission instead of staying next to the new one.
pub fn sweep_expired(state: &Path, keys: &[String], at: u64) -> std::io::Result<()> {
    for key in keys {
        let entries = match fs::read_dir(key_dir(state, key)) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        for entry in entries
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        {
            if read_claim(&entry.path()).is_some_and(|record| !active(&record, at)) {
                match fs::remove_file(entry.path()) {
                    Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                        return Err(error)
                    }
                    _ => {}
                }
            }
        }
    }
    Ok(())
}

/// Remove the claims of `owner` on `keys`; the claims of other owners stay. With the state's lock
/// held an admission cannot interleave; without it the admission still reads the claims before
/// the runs (`Observed::lanes`), so it sees either the claim or the run the launch registered
/// before it released.
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
    lock_for(state, Duration::from_secs(10))
}

/// `lock`, waiting at most `wait`.
pub fn lock_for(
    state: &Path,
    wait: Duration,
) -> Result<pg_worker_core::lockfile::LockGuard, String> {
    fs::create_dir_all(dir(state)).map_err(|e| format!("lane reservation: {e}"))?;
    let anchor = dir(state).join(".lock");
    let end = Instant::now() + wait;
    loop {
        match pg_worker_core::lockfile::try_lock(&anchor) {
            Ok(Some(guard)) => return Ok(guard),
            Ok(None) if Instant::now() < end => {
                #[cfg(test)]
                BUSY_HOOK.with(|hook| hook.borrow().as_ref().map(|hook| hook()));
                std::thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => return Err("lane reservation lock busy".into()),
            Err(e) => return Err(format!("lane reservation: {e}")),
        }
    }
}

/// A fresh owner token for a launch. The sequence number keeps two launches of one process apart
/// whatever the wall clock does, so an owner never names two launches' claims.
pub fn new_owner() -> String {
    static SEQUENCE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let sequence = SEQUENCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("launch-{}-{nanos:x}-{sequence}", std::process::id())
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
#[path = "lane_reservation_tests.rs"]
mod tests;
