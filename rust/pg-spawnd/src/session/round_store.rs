//! Round telemetry store: one JSON row per writer round, replaced on every re-read.

use super::round_time::Cause;
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{fs, io, path::Path};

/// Replace or add each row by its `round_key`; one file, one row per round.
pub fn upsert(path: &Path, rows: &[Value]) -> io::Result<()> {
    let mut all: Vec<Value> = fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    for row in rows {
        match all
            .iter_mut()
            .find(|old| old["round_key"] == row["round_key"])
        {
            Some(old) => *old = row.clone(),
            None => all.push(row.clone()),
        }
    }
    let text: String = all.iter().map(|row| format!("{row}\n")).collect();
    atomic_write_private(path, text.as_bytes())
}

pub fn read(path: &Path) -> Vec<Value> {
    fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

/// Per host: median wall minutes of a round and the share of each cause (between-round
/// time included), in percent.
pub fn digest(rows: &[Value]) -> Vec<Value> {
    let mut hosts: Vec<&str> = rows.iter().filter_map(|r| r["host"].as_str()).collect();
    hosts.sort_unstable();
    hosts.dedup();
    hosts
        .into_iter()
        .map(|host| {
            let mine: Vec<&Value> = rows.iter().filter(|r| r["host"] == host).collect();
            let mut walls: Vec<i64> = mine.iter().filter_map(|r| r["wall_ms"].as_i64()).collect();
            walls.sort_unstable();
            let median = match walls.len() {
                0 => 0.0,
                n if n % 2 == 1 => walls[n / 2] as f64,
                n => (walls[n / 2 - 1] + walls[n / 2]) as f64 / 2.0,
            };
            // Shares count the wait between rounds too; the median stays on wall time.
            let between: i64 = mine
                .iter()
                .filter_map(|r| r["causes"][Cause::BetweenRounds.name()].as_i64())
                .sum();
            let total: i64 = walls.iter().sum::<i64>() + between;
            let share: serde_json::Map<String, Value> = Cause::ALL
                .iter()
                .map(|cause| {
                    let ms: i64 = mine
                        .iter()
                        .filter_map(|r| r["causes"][cause.name()].as_i64())
                        .sum();
                    let pct = if total > 0 {
                        ms as f64 * 100.0 / total as f64
                    } else {
                        0.0
                    };
                    (cause.name().to_owned(), json!((pct * 10.0).round() / 10.0))
                })
                .collect();
            json!({"host": host, "rounds": mine.len(),
                "median_round_min": (median / 6_000.0).round() / 10.0, "share_pct": share})
        })
        .collect()
}

/// The digest line of one host: `host: N rounds, median M min, wait_test 41% ...`.
pub fn digest_line(entry: &Value) -> String {
    let mut shares: Vec<(String, f64)> = entry["share_pct"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(k, v)| Some((k.clone(), v.as_f64()?)))
        .filter(|(_, v)| *v > 0.0)
        .collect();
    shares.sort_by(|a, b| b.1.total_cmp(&a.1));
    let parts: Vec<String> = shares.iter().map(|(k, v)| format!("{k} {v}%")).collect();
    format!(
        "{}: {} rounds, median {} min; {}",
        entry["host"].as_str().unwrap_or("?"),
        entry["rounds"],
        entry["median_round_min"],
        parts.join(", ")
    )
}
