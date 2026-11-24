//! Alert when review seats keep ending without a verdict for one cause.
//!
//! `plangraph review` sees every seat outcome, whichever spawner (Python or Rust) ran it, so the
//! one watcher lives here. Seats are booked in `no_verdict.jsonl` and alerts in `alerts.jsonl`
//! of the review state directory; two seats with different run ids and the same normalised cause
//! inside the window are one incident, and the incidents of a cause do not depend on the order
//! in which the seats were booked.
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
    // The incidents of one cause are a pure function of the booked seats, never of their booking
    // order: seats are clustered chronologically, a cluster spans at most one window from its
    // first seat, and a cluster of REPEAT seats is one incident stamped with its latest terminal
    // time. `alerts.jsonl` holds the current incidents: the lines of this cause are replaced by
    // the recomputed set, so every booking order ends in the same file.
    let mut same: Vec<(i64, String)> = Vec::new();
    for row in rows(&seats) {
        let id = row["run_id"].as_str().unwrap_or_default().to_owned();
        if let (true, Some(ts)) = (row["reason"] == cause.as_str(), row["ts"].as_i64()) {
            if !same.iter().any(|(_, seen)| *seen == id) {
                same.push((ts, id));
            }
        }
    }
    same.sort();
    let mut incidents: Vec<Value> = Vec::new();
    let mut start = 0;
    while start < same.len() {
        let end = same[start..]
            .iter()
            .take_while(|(ts, _)| ts - same[start].0 <= WINDOW_S)
            .count()
            + start;
        let cluster = &same[start..end];
        start = end;
        if cluster.len() >= REPEAT {
            let ids: Vec<&String> = cluster.iter().map(|(_, id)| id).collect();
            incidents.push(json!({
                "ts": cluster[cluster.len() - 1].0, "first": cluster[0].0,
                "kind": "no_verdict_repeat", "reason": cause, "seats": ids,
            }));
        }
    }
    let before = rows(&alerts);
    let mut kept: Vec<Value> = before
        .iter()
        .filter(|row| row["reason"] != cause.as_str())
        .cloned()
        .collect();
    kept.extend(incidents.iter().cloned());
    let mut text = String::new();
    for row in &kept {
        text.push_str(&row.to_string());
        text.push('\n');
    }
    if kept.is_empty() && !alerts.exists() {
        return None;
    }
    crate::atomic::write(&alerts, text.as_bytes()).ok()?;
    // Announce an incident the first time it appears (keyed by its first seat).
    let fresh = incidents.into_iter().find(|incident| {
        !before
            .iter()
            .any(|row| row["reason"] == cause.as_str() && row["first"] == incident["first"])
    })?;
    eprintln!(
        "no_verdict_alert {} review seats ended without a verdict: {cause}",
        fresh["seats"].as_array().map_or(0, Vec::len)
    );
    Some(fresh)
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

    fn permutations(items: &[usize]) -> Vec<Vec<usize>> {
        if items.len() <= 1 {
            return vec![items.to_vec()];
        }
        let mut all = Vec::new();
        for (index, item) in items.iter().enumerate() {
            let mut rest = items.to_vec();
            rest.remove(index);
            for mut tail in permutations(&rest) {
                tail.insert(0, *item);
                all.push(tail);
            }
        }
        all
    }

    fn alerts_after(order: &[usize], seats: &[(&str, i64)]) -> Vec<(i64, Vec<String>)> {
        let dir = tempfile::tempdir().unwrap();
        for index in order {
            note(dir.path(), seats[*index].0, CAUSE, seats[*index].1);
        }
        let text = fs::read_to_string(dir.path().join("alerts.jsonl")).unwrap_or_default();
        let mut rows: Vec<(i64, Vec<String>)> = text
            .lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap())
            .map(|v| {
                let ids = v["seats"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|i| i.as_str().unwrap().to_owned());
                (v["ts"].as_i64().unwrap(), ids.collect())
            })
            .collect();
        rows.sort();
        rows
    }

    #[test]
    fn the_alert_file_is_the_same_in_every_booking_order() {
        // Review case: seats ended at 1000, 1001, 2801 and 2802. 1001 and 2801 are 1800 s apart, but
        // the incidents are [1000, 1001] and [2801, 2802] whichever order the seats were booked in.
        let four = [
            ("run-a", 1000),
            ("run-b", 1001),
            ("run-c", 2801),
            ("run-d", 2802),
        ];
        let expected = vec![
            (1001, vec!["run-a".to_owned(), "run-b".to_owned()]),
            (2802, vec!["run-c".to_owned(), "run-d".to_owned()]),
        ];
        let orders = permutations(&[0, 1, 2, 3]);
        assert_eq!(orders.len(), 24);
        for order in orders {
            assert_eq!(alerts_after(&order, &four), expected, "order {order:?}");
        }
        // The earlier three-seat case: one incident [1000, 2800], 2801 starts a new window.
        let three = [("run-a", 1000), ("run-b", 2800), ("run-c", 2801)];
        for order in permutations(&[0, 1, 2]) {
            let rows = alerts_after(&order, &three);
            assert_eq!(
                rows,
                vec![(2800, vec!["run-a".to_owned(), "run-b".to_owned()])],
                "order {order:?}"
            );
        }
    }

    #[test]
    fn a_reported_incident_never_spans_more_than_one_window() {
        let dir = tempfile::tempdir().unwrap();
        assert!(note(dir.path(), "run-a", CAUSE, 1000).is_none());
        let alert = note(dir.path(), "run-b", CAUSE, 2500).expect("1500 s apart is one incident");
        assert_eq!(alert["seats"], json!(["run-a", "run-b"]));
        // 4000 is 1500 s after 2500 but 3000 s after the incident's first seat: not part of it
        assert!(note(dir.path(), "run-c", CAUSE, 4000).is_none());
        let alerts = fs::read_to_string(dir.path().join("alerts.jsonl")).unwrap();
        assert_eq!(alerts.lines().count(), 1, "{alerts}");
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
    fn concurrent_seats_leave_one_incident_with_every_seat() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().to_owned();
        let handles: Vec<_> = (0..6)
            .map(|n| {
                let path = path.clone();
                std::thread::spawn(move || {
                    note(&path, &format!("run-{n:02x}"), CAUSE, 1000 + n);
                })
            })
            .collect();
        for handle in handles {
            handle.join().unwrap();
        }
        let text = fs::read_to_string(path.join("alerts.jsonl")).unwrap();
        assert_eq!(text.lines().count(), 1, "{text}");
        let alert: Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
        assert_eq!(alert["seats"].as_array().unwrap().len(), 6, "{text}");
    }
}
