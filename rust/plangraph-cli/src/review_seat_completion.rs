//! Deferred evidence for one live `plangraph review` seat. Only the caller may issue an approval.
use super::evaluate_seat_record;
use pg_worker_core::{
    review_completion::{self, GRACE_SECONDS},
    review_split::SeatVerdict,
    run::{
        finalize::now,
        record::{releases_ownership, run_dir, RunRecord, Status},
        record_io::atomic_write_private,
    },
};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

// Compatibility with spawners that predate the run-record deadline.
const DEFAULT_REVIEW_SECONDS: f64 = 25.0 * 60.0;

pub(super) fn defer(
    state: &Path,
    head: &str,
    id: &str,
    record: &Value,
) -> Result<(PathBuf, f64), String> {
    let directory = run_dir(state, id).map_err(|e| e.to_string())?;
    let expected_end = record["expected_end"]
        .as_f64()
        .unwrap_or_else(|| record["started"].as_f64().unwrap_or_else(now) + DEFAULT_REVIEW_SECONDS);
    review_completion::start([
        "review".as_ref(),
        "--finish-seat".as_ref(),
        state.as_os_str(),
        head.as_ref(),
        id.as_ref(),
        expected_end.to_string().as_ref(),
    ])
    .map_err(|e| format!("could not start finisher for {id}: {e}"))?;
    Ok((directory.join("late-review.txt"), expected_end))
}

pub(super) fn finish(args: &[String]) -> Result<(), String> {
    let [state, head, id, expected_end] = args else {
        return Err("invalid seat completion request".into());
    };
    if head.len() != 40 || !head.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid seat review head".into());
    }
    let state = Path::new(state);
    let dir = run_dir(state, id).map_err(|e| e.to_string())?;
    let deadline = expected_end
        .parse::<f64>()
        .ok()
        .filter(|n| n.is_finite())
        .ok_or("invalid seat review deadline")?
        + GRACE_SECONDS;
    loop {
        if let Ok(record) = RunRecord::read(state, id) {
            let record = record.to_value();
            let terminal = record["status"].as_str().is_some_and(|status| {
                Status::parse(status).is_some_and(Status::is_terminal) || releases_ownership(status)
            });
            if record["run_id"] == id.as_str() && terminal {
                let seat = evaluate_seat_record(1, 1, head, &[], &record, Some(&dir));
                // The same booking path as the foreground review: a seat that was pending and
                // ends without a verdict counts toward the repeat alert at its terminal time.
                super::no_verdict::book(state, &seat, seat.ended.unwrap_or(now() as i64));
                let verdict = match (seat.status.as_str(), seat.exit_code, seat.verdict) {
                    ("completed", 0, Some(SeatVerdict::Pass)) if seat.passed => "PASS",
                    ("completed", 0, Some(SeatVerdict::RequestChanges)) => "REQUEST_CHANGES",
                    ("completed", 0, None) => "MISSING",
                    _ => "ERROR",
                };
                let text = format!(
                    "RUN: {id}\nHEAD: {head}\nSTATUS: {}\nVERDICT: {verdict}\n",
                    seat.status
                );
                return atomic_write_private(&dir.join("late-review.txt"), text.as_bytes())
                    .map_err(|e| e.to_string());
            }
        }
        if now() >= deadline {
            return Err(format!("review seat {id} did not reach terminal status"));
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}
