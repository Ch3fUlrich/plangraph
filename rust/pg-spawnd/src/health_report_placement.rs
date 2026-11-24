//! Per-hour placement telemetry: writer runs started per host and role with the free memory the
//! admission recorded, and an alert when one host holds every writer of an hour while another admits.
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// A host with at least this much free memory (MB) admits a writer.
const ADMIT_FREE_MB: i64 = 4096;
/// Writers in an hour before concentration on one host is a finding.
const MIN_WRITERS: u64 = 2;

fn host_of(record: &Value) -> Option<&str> {
    record["placement"]["host"]
        .as_str()
        .or_else(|| record["host"].as_str())
        .filter(|h| !h.is_empty())
}

fn started(record: &Value) -> Option<i64> {
    record["started"]
        .as_f64()
        .filter(|t| t.is_finite())
        .map(|t| t as i64)
}

/// `(rows, alerts)` for the hours of `[since, until)`. A row is `{hour, host, role, runs, free_mb}`
/// (`free_mb` is the last recorded free memory of the host in that hour, or null).
pub(crate) fn placement_hourly(
    records: &[Value],
    since: i64,
    until: i64,
) -> (Vec<Value>, Vec<String>) {
    // (hour, host, role) -> (runs, free_mb), and the latest free memory per host by time.
    let mut cells = BTreeMap::<(i64, String, String), (u64, Option<i64>)>::new();
    let mut free_at = BTreeMap::<String, (i64, i64)>::new();
    for record in records {
        let (Some(host), Some(ts)) = (host_of(record), started(record)) else {
            continue;
        };
        if ts < since || ts >= until || matches!(record["status"].as_str(), Some("dry_run")) {
            continue;
        }
        let role = record["role"].as_str().unwrap_or("implement");
        let free = record["placement"]["free_mb"]
            .as_f64()
            .filter(|m| m.is_finite())
            .map(|m| m as i64);
        let cell = cells
            .entry((ts - ts.rem_euclid(3600), host.into(), role.into()))
            .or_insert((0, None));
        cell.0 += 1;
        cell.1 = free.or(cell.1);
        if let Some(free) = free {
            if free_at.get(host).is_none_or(|(at, _)| *at <= ts) {
                free_at.insert(host.into(), (ts, free));
            }
        }
    }
    let rows = cells
        .iter()
        .map(|((hour, host, role), (runs, free))| {
            json!({"hour": hour, "host": host, "role": role, "runs": runs, "free_mb": free})
        })
        .collect();
    let mut alerts = Vec::new();
    let hours: std::collections::BTreeSet<i64> = cells.keys().map(|(h, _, _)| *h).collect();
    for hour in hours {
        let writers: Vec<(&String, u64)> = cells
            .iter()
            .filter(|((h, _, role), _)| *h == hour && role == "implement")
            .map(|((_, host, _), (runs, _))| (host, *runs))
            .collect();
        let total: u64 = writers.iter().map(|(_, runs)| runs).sum();
        let [(holder, _)] = writers[..] else {
            continue;
        };
        if total < MIN_WRITERS {
            continue;
        }
        if let Some((other, (_, free))) = free_at
            .iter()
            .find(|(host, (_, free))| *host != holder && *free >= ADMIT_FREE_MB)
        {
            alerts.push(format!(
                "ALERT writers concentrated: hour={hour} host={holder} writers={total} while host={other} admits free_mb={free}"
            ));
        }
    }
    (rows, alerts)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(host: &str, role: &str, ts: i64, free: i64) -> Value {
        json!({"role": role, "started": ts, "placement": {"host": host, "free_mb": free}})
    }

    #[test]
    fn rows_count_runs_per_hour_host_and_role_with_the_last_free_memory() {
        let records = [
            run("a", "implement", 3600, 9000),
            run("a", "implement", 3700, 8000),
            run("b", "review", 3800, 20000),
            run("a", "implement", 7300, 7000),
        ];
        let (rows, _) = placement_hourly(&records, 0, 10_000);
        assert_eq!(
            rows,
            [
                json!({"hour": 3600, "host": "a", "role": "implement", "runs": 2, "free_mb": 8000}),
                json!({"hour": 3600, "host": "b", "role": "review", "runs": 1, "free_mb": 20000}),
                json!({"hour": 7200, "host": "a", "role": "implement", "runs": 1, "free_mb": 7000}),
            ]
        );
    }

    #[test]
    fn every_writer_on_one_host_while_another_admits_is_an_alert() {
        let all_one = [
            run("a", "implement", 3600, 2000),
            run("a", "implement", 3700, 2000),
            run("b", "review", 3800, 30000),
        ];
        let (_, alerts) = placement_hourly(&all_one, 0, 10_000);
        assert_eq!(alerts.len(), 1, "{alerts:?}");
        assert!(alerts[0].contains("host=a") && alerts[0].contains("host=b admits"));
        // Writers on two hosts, one writer only, or no admitting host: no alert.
        let spread = [
            run("a", "implement", 3600, 2000),
            run("b", "implement", 3700, 30000),
        ];
        assert!(placement_hourly(&spread, 0, 10_000).1.is_empty());
        let single = [
            run("a", "implement", 3600, 2000),
            run("b", "review", 3800, 30000),
        ];
        assert!(placement_hourly(&single, 0, 10_000).1.is_empty());
        let none_admit = [
            run("a", "implement", 3600, 2000),
            run("a", "implement", 3700, 2000),
            run("b", "review", 3800, 1000),
        ];
        assert!(placement_hourly(&none_admit, 0, 10_000).1.is_empty());
    }

    #[test]
    fn runs_outside_the_window_and_dry_runs_are_not_counted() {
        let mut dry = run("a", "implement", 3700, 2000);
        dry["status"] = json!("dry_run");
        let records = [run("a", "implement", 50_000, 2000), dry];
        assert!(placement_hourly(&records, 0, 10_000).0.is_empty());
    }
}
