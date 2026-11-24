//! Alert when review seats keep ending without a verdict for one cause.
//!
//! `plangraph review` sees every seat outcome, whichever spawner (Python or Rust) ran it, so the
//! one watcher lives here. Seats are booked in `no_verdict.jsonl` and alerts in `alerts.jsonl`
//! of the review state directory; two seats with different run ids and the same normalised cause
//! inside the window raise one alert per cause per window.
use super::SeatOutcome;
use pg_worker_core::lockfile::{try_lock, LockGuard};
use pg_worker_core::review_split::SeatVerdict;
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

/// Takes the worker-core advisory lock on `<dir>/no_verdict.lock`; the OS releases it with the
/// process, so a killed holder never silences the alert and the lock file is never unlinked.
fn take_lock(dir: &Path) -> Option<LockGuard> {
    let path = dir.join("no_verdict.lock");
    let began = Instant::now();
    loop {
        if let Ok(Some(guard)) = try_lock(&path) {
            return Some(guard);
        }
        if began.elapsed() > Duration::from_secs(3) {
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Whether this outcome is a seat that ended without a verdict: it neither passed nor is
/// still pending, and the run either did not complete or its answer had no verdict. A completed
/// run with an explicit verdict has one whatever its exit code (a denied-tools OpenCode review
/// ends exit 1 with a final REQUEST_CHANGES).
pub fn is_no_verdict(seat: &SeatOutcome) -> bool {
    !seat.passed
        && seat.verdict != Some(SeatVerdict::Pending)
        && (seat.status != "completed" || seat.verdict.is_none())
}

/// The one booking path of a terminal seat, used by the foreground review and by the deferred
/// finisher of a seat that was pending: books a no-verdict outcome at its terminal time.
pub fn book(state: &Path, seat: &SeatOutcome, ended_at: i64) -> Option<Value> {
    if !is_no_verdict(seat) {
        return None;
    }
    note(
        state,
        &seat.run_id,
        seat.reason.as_deref().unwrap_or("no_verdict_line"),
        ended_at,
    )
}

/// Book one run that ended without a verdict at `now` (its terminal time); returns the alert when it completes a repeat.
pub fn note(dir: &Path, run_id: &str, reason: &str, now: i64) -> Option<Value> {
    let cause = normalise(reason);
    fs::create_dir_all(dir).ok()?;
    let _lock = take_lock(dir)?;
    let (seats, alerts) = (dir.join("no_verdict.jsonl"), dir.join("alerts.jsonl"));
    // Booking a run is idempotent: a retry never moves its terminal time or counts it twice.
    if rows(&seats).iter().any(|row| row["run_id"] == run_id) {
        return None;
    }
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
        // By terminal time, whichever seat was booked first: within the window on either side.
        let recent = row["ts"]
            .as_i64()
            .is_some_and(|ts| (now - ts).abs() <= WINDOW_S);
        let id = row["run_id"].as_str().unwrap_or_default().to_owned();
        if recent && row["reason"] == cause.as_str() && !ids.contains(&id) {
            ids.push(id);
        }
    }
    let alerted = rows(&alerts).iter().any(|row| {
        row["reason"] == cause.as_str()
            && row["ts"]
                .as_i64()
                .is_some_and(|ts| (now - ts).abs() <= WINDOW_S)
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
    fn a_retried_booking_keeps_the_first_terminal_time_and_never_counts_twice() {
        let dir = tempfile::tempdir().unwrap();
        assert!(note(dir.path(), "run-aa", CAUSE, 1000).is_none());
        assert!(note(dir.path(), "run-aa", CAUSE, 1010).is_none());
        assert!(!dir.path().join("alerts.jsonl").exists());
        // run-aa ended at 1000: a seat booked at 3000 is outside its 30-minute window, even if
        // run-aa is rebooked just before it.
        assert!(note(dir.path(), "run-aa", CAUSE, 2999).is_none());
        assert!(note(dir.path(), "run-bb", CAUSE, 3000).is_none());
        assert!(!dir.path().join("alerts.jsonl").exists());
    }

    #[test]
    fn the_window_is_by_terminal_time_whichever_seat_is_booked_first() {
        let dir = tempfile::tempdir().unwrap();
        // The later seat is booked first: 2000 s apart is outside the 30 minutes.
        assert!(note(dir.path(), "run-late", CAUSE, 3000).is_none());
        assert!(note(dir.path(), "run-early", CAUSE, 1000).is_none());
        assert!(!dir.path().join("alerts.jsonl").exists());
        // 1000 s apart in reverse arrival order is a repeat.
        assert!(note(dir.path(), "run-mid", CAUSE, 2000).is_some());
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
