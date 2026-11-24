//! Alert when review seats keep ending without a verdict for one cause.
//!
//! `plangraph review` sees every seat outcome, whichever spawner (Python or Rust) ran it, so the
//! one watcher lives here. Seats are booked in `no_verdict.jsonl` and alerts in `alerts.jsonl`
//! of the review state directory; two seats with different run ids and the same normalised cause
//! inside the window raise one alert per cause per window.
use regex::Regex;
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant};

pub const WINDOW_S: i64 = 1800;
pub const REPEAT: usize = 2;

/// The cause with run ids, paths and numbers removed, so the same cause groups across seats.
pub fn normalise(reason: &str) -> String {
    let ids = Regex::new(r"run-[0-9a-f]+|\S*/\S*").unwrap();
    let digits = Regex::new(r"[0-9]+").unwrap();
    let line = reason.split_whitespace().collect::<Vec<_>>().join(" ");
    let flat = digits
        .replace_all(&ids.replace_all(&line, "<x>"), "#")
        .into_owned();
    flat.chars().take(160).collect()
}

fn rows(path: &Path) -> Vec<Value> {
    fs::read_to_string(path)
        .map(|text| {
            text.lines()
                .filter_map(|l| serde_json::from_str(l).ok())
                .collect()
        })
        .unwrap_or_default()
}

/// Holds `<dir>/no_verdict.lock` (created exclusively) so the count and the alert write are one step.
struct Lock(std::path::PathBuf);

impl Lock {
    fn take(dir: &Path) -> Option<Lock> {
        let path = dir.join("no_verdict.lock");
        let began = Instant::now();
        loop {
            if OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
                .is_ok()
            {
                return Some(Lock(path));
            }
            // A lock left by a killed process must not silence the alert for good.
            let stale = fs::metadata(&path)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.elapsed().ok())
                .is_some_and(|age| age > Duration::from_secs(30));
            if stale {
                let _ = fs::remove_file(&path);
            } else if began.elapsed() > Duration::from_secs(3) {
                return None;
            } else {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

/// Book one seat that ended without a verdict; returns the alert when this seat completes a repeat.
pub fn note(dir: &Path, run_id: &str, reason: &str, now: i64) -> Option<Value> {
    let cause = normalise(reason);
    fs::create_dir_all(dir).ok()?;
    let _lock = Lock::take(dir)?;
    let (seats, alerts) = (dir.join("no_verdict.jsonl"), dir.join("alerts.jsonl"));
    let mut file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(&seats)
        .ok()?;
    writeln!(
        file,
        "{}",
        json!({"ts": now, "run_id": run_id, "reason": cause})
    )
    .ok()?;
    let mut ids: Vec<String> = Vec::new();
    for row in rows(&seats) {
        let recent = row["ts"].as_i64().is_some_and(|ts| now - ts <= WINDOW_S);
        let id = row["run_id"].as_str().unwrap_or_default().to_owned();
        if recent && row["reason"] == cause.as_str() && !ids.contains(&id) {
            ids.push(id);
        }
    }
    let alerted = rows(&alerts).iter().any(|row| {
        row["reason"] == cause.as_str() && row["ts"].as_i64().is_some_and(|ts| now - ts <= WINDOW_S)
    });
    if ids.len() < REPEAT || alerted {
        return None;
    }
    let alert = json!({"ts": now, "kind": "no_verdict_repeat", "reason": cause, "seats": ids});
    let mut out = OpenOptions::new()
        .append(true)
        .create(true)
        .open(&alerts)
        .ok()?;
    writeln!(out, "{alert}").ok()?;
    eprintln!(
        "no_verdict_alert {} review seats ended without a verdict: {cause}",
        ids.len()
    );
    Some(alert)
}

#[cfg(test)]
mod tests {
    use super::*;

    const CAUSE: &str = "harness_not_found opencode /usr/bin:/bin";

    #[test]
    fn two_distinct_seats_with_one_cause_raise_one_alert() {
        let dir = tempfile::tempdir().unwrap();
        assert!(note(dir.path(), "run-aa", CAUSE, 1000).is_none());
        let alert = note(dir.path(), "run-bb", CAUSE, 1600).expect("second seat alerts");
        assert_eq!(alert["seats"], json!(["run-aa", "run-bb"]));
        assert!(
            note(dir.path(), "run-cc", CAUSE, 1700).is_none(),
            "one alert per window"
        );
        let alerts = fs::read_to_string(dir.path().join("alerts.jsonl")).unwrap();
        assert_eq!(alerts.lines().count(), 1);
    }

    #[test]
    fn a_retried_save_of_one_run_is_not_a_second_seat() {
        let dir = tempfile::tempdir().unwrap();
        assert!(note(dir.path(), "run-aa", CAUSE, 1000).is_none());
        assert!(note(dir.path(), "run-aa", CAUSE, 1010).is_none());
        assert!(!dir.path().join("alerts.jsonl").exists());
    }

    #[test]
    fn other_causes_and_old_seats_raise_nothing() {
        let dir = tempfile::tempdir().unwrap();
        assert!(note(dir.path(), "run-aa", "provider usage limit", 0).is_none());
        assert!(note(dir.path(), "run-bb", CAUSE, 10).is_none());
        assert!(note(dir.path(), "run-cc", "provider usage limit", 4000).is_none());
    }

    #[test]
    fn concurrent_seats_raise_exactly_one_alert() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().to_owned();
        let handles: Vec<_> = (0..6)
            .map(|n| {
                let path = path.clone();
                std::thread::spawn(move || {
                    note(&path, &format!("run-{n:02x}"), CAUSE, 1000 + n).is_some()
                })
            })
            .collect();
        let alerts = handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .filter(|a| *a)
            .count();
        assert_eq!(alerts, 1);
        assert_eq!(
            fs::read_to_string(path.join("alerts.jsonl"))
                .unwrap()
                .lines()
                .count(),
            1
        );
    }
}
