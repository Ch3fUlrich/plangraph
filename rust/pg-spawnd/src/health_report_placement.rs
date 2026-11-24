//! Per-hour placement telemetry: writer runs started per host and role with the free memory the
//! admission recorded, and an alert when one host holds every writer of an hour while another admits.
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// Free memory (MB) a host needs to admit a writer when the host policy names no floor for it.
const DEFAULT_FLOOR_MB: i64 = 4096;
/// Writers in an hour before concentration on one host is a finding.
const MIN_WRITERS: u64 = 2;

/// Per-host memory floors (MB) from the host policy file named by `FLEET_HOST_POLICY`, if any.
pub(crate) fn policy_floors() -> BTreeMap<String, i64> {
    let policy = std::env::var_os("FLEET_HOST_POLICY")
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str::<Value>(&text).ok());
    policy
        .as_ref()
        .and_then(|p| p["hosts"].as_array())
        .into_iter()
        .flatten()
        .filter_map(|host| {
            let gib = host["memory_floor_gib"]
                .as_f64()
                .filter(|g| g.is_finite())?;
            Some((
                host["name"].as_str()?.to_owned(),
                (gib * 1024.0).ceil() as i64,
            ))
        })
        .collect()
}

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

/// A launch that never started work: refusals, queue entries and dry runs are no launches.
fn launched(record: &Value) -> bool {
    !matches!(
        record["status"].as_str(),
        Some("dry_run" | "refused" | "queued" | "skipped")
    )
}

/// `(rows, alerts)` for the hours of `[since, until)`. A row is `{hour, host, role, runs, free_mb}`
/// (`free_mb` is the last free memory the admission recorded for the host in that hour, or null).
/// Admission is judged only from samples of the same hour; a host with no sample that hour is unknown
/// (an idle host leaves no sample) and is never assumed to admit.
pub(crate) fn placement_hourly(
    records: &[Value],
    floors: &BTreeMap<String, i64>,
    since: i64,
    until: i64,
) -> (Vec<Value>, Vec<String>) {
    let mut cells = BTreeMap::<(i64, String, String), (u64, Option<i64>)>::new();
    // (hour, host) -> (time of the sample, free_mb): the latest sample of that hour.
    let mut samples = BTreeMap::<(i64, String), (i64, i64)>::new();
    for record in records {
        let (Some(host), Some(ts)) = (host_of(record), started(record)) else {
            continue;
        };
        if ts < since || ts >= until {
            continue;
        }
        let hour = ts - ts.rem_euclid(3600);
        let free = record["placement"]["free_mb"]
            .as_f64()
            .filter(|m| m.is_finite())
            .map(|m| m as i64);
        if let Some(free) = free {
            let sample = samples.entry((hour, host.into())).or_insert((ts, free));
            if sample.0 <= ts {
                *sample = (ts, free);
            }
        }
        if record["status"].as_str() == Some("dry_run") || !launched(record) {
            continue;
        }
        let role = record["role"].as_str().unwrap_or("implement");
        let cell = cells
            .entry((hour, host.into(), role.into()))
            .or_insert((0, None));
        cell.0 += 1;
        cell.1 = free.or(cell.1);
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
        let admits = samples.iter().find(|((h, host), (_, free))| {
            *h == hour
                && host != holder
                && *free >= floors.get(host).copied().unwrap_or(DEFAULT_FLOOR_MB)
        });
        if let Some(((_, other), (_, free))) = admits {
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

    fn none() -> BTreeMap<String, i64> {
        BTreeMap::new()
    }

    #[test]
    fn rows_count_runs_per_hour_host_and_role_with_the_last_free_memory() {
        let records = [
            run("a", "implement", 3600, 9000),
            run("a", "implement", 3700, 8000),
            run("b", "review", 3800, 20000),
            run("a", "implement", 7300, 7000),
        ];
        let (rows, _) = placement_hourly(&records, &none(), 0, 10_000);
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
        let (_, alerts) = placement_hourly(&all_one, &none(), 0, 10_000);
        assert_eq!(alerts.len(), 1, "{alerts:?}");
        assert!(alerts[0].contains("host=a") && alerts[0].contains("host=b admits"));
        let spread = [
            run("a", "implement", 3600, 2000),
            run("b", "implement", 3700, 30000),
        ];
        assert!(placement_hourly(&spread, &none(), 0, 10_000).1.is_empty());
        let single = [
            run("a", "implement", 3600, 2000),
            run("b", "review", 3800, 30000),
        ];
        assert!(placement_hourly(&single, &none(), 0, 10_000).1.is_empty());
        let none_admit = [
            run("a", "implement", 3600, 2000),
            run("a", "implement", 3700, 2000),
            run("b", "review", 3800, 1000),
        ];
        assert!(placement_hourly(&none_admit, &none(), 0, 10_000)
            .1
            .is_empty());
    }

    #[test]
    fn admission_uses_the_hosts_own_floor_and_only_the_same_hours_sample() {
        let records = [
            run("a", "implement", 3600, 2000),
            run("a", "implement", 3700, 2000),
            run("b", "review", 3800, 9000),
        ];
        // b's floor is 12 GiB: 9000 MB does not admit; with an 8 GiB floor it does.
        let high = BTreeMap::from([("b".to_owned(), 12 * 1024)]);
        assert!(placement_hourly(&records, &high, 0, 10_000).1.is_empty());
        let low = BTreeMap::from([("b".to_owned(), 8 * 1024)]);
        assert_eq!(placement_hourly(&records, &low, 0, 10_000).1.len(), 1);
        // A later hour's sample of b never claims b admitted an earlier hour.
        let later = [
            run("a", "implement", 3600, 2000),
            run("a", "implement", 3700, 2000),
            run("b", "review", 7300, 30000),
        ];
        assert!(placement_hourly(&later, &none(), 0, 10_000).1.is_empty());
    }

    #[test]
    fn refusals_queue_entries_and_dry_runs_are_not_launches() {
        let mut refused = run("a", "implement", 3600, 2000);
        refused["status"] = json!("refused");
        let mut dry = run("a", "implement", 3700, 2000);
        dry["status"] = json!("dry_run");
        let records = [refused, dry, run("b", "review", 3800, 30000)];
        let (rows, alerts) = placement_hourly(&records, &none(), 0, 10_000);
        assert_eq!(rows.len(), 1, "{rows:?}");
        assert!(alerts.is_empty());
    }

    #[test]
    fn runs_outside_the_window_are_not_counted() {
        let records = [run("a", "implement", 50_000, 2000)];
        assert!(placement_hourly(&records, &none(), 0, 10_000).0.is_empty());
    }
}
