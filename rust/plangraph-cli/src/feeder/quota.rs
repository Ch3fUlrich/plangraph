use super::adapters::{quota_snapshots, Env};
use super::quota_publish::{export, publish_limit, publishable, push_snapshot, split_options};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
fn field<'a>(r: &'a Value, names: &[&str]) -> Option<&'a Value> {
    names.iter().find_map(|n| r.get(n).filter(|v| !v.is_null()))
}
fn identity(row: &Value) -> Option<(&str, &str)> {
    let name = row["provider"].as_str()?;
    let (provider, suffix) = name.split_once(':').unwrap_or((name, ""));
    let account = field(row, &["connection_id", "connectionId"])
        .and_then(Value::as_str)
        .unwrap_or(suffix);
    Some((provider, account))
}
fn observed(row: &Value) -> Option<&Value> {
    field(row, &["created_at", "createdAt", "timestamp"])
}
fn window(row: &Value) -> Option<&str> {
    field(row, &["window_key", "windowKey"]).and_then(Value::as_str)
}
fn remaining_percent(row: &Value) -> Option<f64> {
    if field(row, &["is_exhausted", "isExhausted"])
        .is_some_and(|v| v == true || v == 1 || v == "1" || v == "true")
    {
        return Some(0.0);
    }
    field(
        row,
        &[
            "remaining_percentage",
            "remainingPercentage",
            "remainingPct",
        ],
    )
    .and_then(Value::as_f64)
    .filter(|q| (0.0..=100.0).contains(q))
}
fn stamp(v: &Value) -> Option<i64> {
    if let Some(n) = v.as_f64() {
        return n.is_finite().then_some(if n > 1e11 {
            (n / 1000.0) as i64
        } else {
            n as i64
        });
    }
    let s = v.as_str()?;
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|t| t.timestamp())
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .map(|t| t.and_utc().timestamp())
        })
}
/// Latest snapshot per account/window; stale or malformed windows disable that
/// account, never an independent healthy account. Hourly store default: 3 hours.
pub(super) fn remaining(rows: &Value, now: i64, max_age: i64) -> Option<f64> {
    remaining_for(rows, "codex", now, max_age)
}

pub(crate) fn remaining_for(rows: &Value, target: &str, now: i64, max_age: i64) -> Option<f64> {
    let mut latest: BTreeMap<(String, String), (i64, Option<f64>)> = BTreeMap::new();
    let mut invalid = BTreeSet::new();
    for r in rows.as_array()? {
        let Some((provider, account)) = identity(r) else {
            continue;
        };
        if provider != target {
            continue;
        }
        let Some(window) = window(r) else {
            invalid.insert(account.to_owned());
            continue;
        };
        let Some(at) = observed(r).and_then(stamp) else {
            invalid.insert(account.to_owned());
            continue;
        };
        let q = if at > now || now.saturating_sub(at) > max_age {
            None
        } else {
            remaining_percent(r)
        };
        let value = latest
            .entry((account.into(), window.into()))
            .or_insert((at, q));
        if at >= value.0 {
            *value = (at, q);
        }
    }
    let mut accounts: BTreeMap<String, Option<f64>> = BTreeMap::new();
    for ((a, _), (_, q)) in latest {
        accounts
            .entry(a)
            .and_modify(|v| *v = v.zip(q).map(|(a, b)| a.min(b)))
            .or_insert(q);
    }
    accounts
        .into_iter()
        .filter(|(a, _)| !invalid.contains(a))
        .filter_map(|(_, q)| q)
        .reduce(f64::max)
}

/// Whether the rows hold any observation of the codex provider (any account).
fn has_codex_rows(rows: &Value) -> bool {
    rows.as_array().is_some_and(|rows| {
        rows.iter()
            .any(|r| identity(r).is_some_and(|(provider, _)| provider == "codex"))
    })
}

/// The center's quota snapshot is missing, unreadable, too old or holds no usable observation.
/// The text is the refusal reason shown to the operator, with the age where one is known.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct StaleQuota(pub String);
fn stale(why: impl std::fmt::Display) -> StaleQuota {
    StaleQuota(format!("quota_stale: {why}"))
}

/// Parse the center's quota snapshot file (`{"observed_at":<unix s>,"rows":[..]}`, the rows in
/// the reader's own format) and accept it only while it is at most `max_age` seconds old.
/// A missing, unreadable or stale file refuses as `quota_stale` and names its age.
pub(super) fn center_rows(
    text: Option<&str>,
    now: i64,
    max_age: i64,
    observation_max_age: i64,
) -> Result<Value, StaleQuota> {
    let doc: Value = text
        .and_then(|t| serde_json::from_str(t).ok())
        .ok_or_else(|| stale("no readable center quota snapshot (age unknown)"))?;
    let at = doc["observed_at"]
        .as_i64()
        .ok_or_else(|| stale("center quota snapshot has no observed_at (age unknown)"))?;
    let age = now.saturating_sub(at);
    if at > now || age > max_age {
        return Err(stale(format!(
            "center quota snapshot is {age}s old (limit {max_age}s)"
        )));
    }
    let rows = doc
        .get("rows")
        .filter(|rows| rows.is_array())
        .cloned()
        .ok_or_else(|| stale("center quota snapshot has no rows (age unknown)"))?;
    // A fresh envelope around old or empty observations is still stale quota: say how old.
    if remaining(&rows, now, observation_max_age).is_none() {
        let newest = newest_codex_age(&rows, now).map_or_else(
            || "no codex observation".to_string(),
            |a| format!("newest {a}s old"),
        );
        return Err(stale(format!(
            "center quota snapshot is {age}s old but holds no usable codex observation ({newest}, limit {observation_max_age}s)"
        )));
    }
    Ok(rows)
}

/// Age in seconds of the newest codex observation in `rows`, if any carries a timestamp.
pub(super) fn newest_codex_age(rows: &Value, now: i64) -> Option<i64> {
    rows.as_array()?
        .iter()
        .filter(|r| identity(r).is_some_and(|(provider, _)| provider == "codex"))
        .filter_map(|r| observed(r).and_then(stamp))
        .max()
        .map(|at| now.saturating_sub(at))
}

/// The quota rows admission reads. A host with its own codex observations (the center) uses
/// them. A host with none, and no quota supplied with its facts, reads the center's snapshot
/// file instead, so quota is never silently null there.
pub(super) fn rows_for_host(
    local: Option<Value>,
    supplied: Option<f64>,
    center: &Path,
    now: i64,
    max_age: i64,
    observation_max_age: i64,
) -> Result<Option<Value>, StaleQuota> {
    if supplied.is_some() || local.as_ref().is_some_and(has_codex_rows) {
        return Ok(local);
    }
    center_rows(
        std::fs::read_to_string(center).ok().as_deref(),
        now,
        max_age,
        observation_max_age,
    )
    .map(Some)
}

/// Print the most recent available observation for each provider, account and window.
pub fn run_status(env: &Env, args: &[String]) -> i32 {
    let mut effective = env.clone();
    let (args, options) = match split_options(args) {
        Ok(split) => split,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let exporting = args.iter().any(|a| a == "--export");
    let limit = if exporting {
        match publish_limit(options.policy.as_deref()) {
            Ok(limit) => limit,
            Err(e) => {
                eprintln!("quota publish: {e}");
                return 2;
            }
        }
    } else {
        0
    };
    match args.as_slice() {
        [] => {}
        [flag, path] if flag == "--db" && !path.is_empty() => {
            effective.insert("QUOTA_SNAPSHOT_DB".into(), path.clone());
        }
        [flag, path] if flag == "--export" && !path.is_empty() => {
            let rows = std::env::current_dir()
                .ok()
                .and_then(|repo| quota_snapshots(&effective, &repo).ok())
                .filter(|rows| rows.is_array());
            let Some(rows) = rows else {
                eprintln!("quota export: reader unavailable");
                return 2;
            };
            let now = chrono::Utc::now().timestamp();
            if let Err(why) = publishable(&rows, now, limit) {
                eprintln!("quota export: {why}");
                return 2;
            }
            if export(&rows, Path::new(path), now).is_err() {
                eprintln!("quota export: cannot write the snapshot file");
                return 2;
            }
            return 0;
        }
        [flag, destination] if flag == "--push" => {
            let rows = std::env::current_dir()
                .ok()
                .and_then(|repo| quota_snapshots(&effective, &repo).ok())
                .filter(|rows| rows.is_array());
            let Some(rows) = rows else {
                eprintln!("quota push: reader unavailable");
                return 2;
            };
            let now = chrono::Utc::now().timestamp();
            return match push_snapshot(
                &rows,
                destination,
                "ssh",
                options.worker_policy.as_deref(),
                now,
            ) {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("{e}");
                    2
                }
            };
        }
        _ => {
            eprintln!(
                "usage: plangraph quota [--db PATH | --export PATH [--policy PATH] | --push SSH_DESTINATION [--worker-policy PATH]]"
            );
            return 2;
        }
    }
    let result = std::env::current_dir()
        .ok()
        .and_then(|repo| quota_snapshots(&effective, &repo).ok());
    let mut latest = BTreeMap::new();
    for row in result
        .as_ref()
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some((provider, account)) = identity(row) else {
            continue;
        };
        let Some(window) = window(row) else { continue };
        let Some(observed) = observed(row) else {
            continue;
        };
        let Some(at) = stamp(observed) else { continue };
        if [provider, account, window]
            .iter()
            .any(|s| s.chars().any(char::is_control))
        {
            continue;
        }
        let entry = latest
            .entry((provider, account, window))
            .or_insert((at, row, observed));
        if at >= entry.0 {
            *entry = (at, row, observed);
        }
    }
    if latest.is_empty() {
        println!("quota unknown");
    } else {
        println!("provider\taccount\twindow\tused / limit\tobserved at");
        for ((provider, account, window), (_, row, observed)) in latest {
            let usage = remaining_percent(row).map_or_else(
                || "unknown".into(),
                |left| {
                    let used = format!("{:.1}", 100.0 - left);
                    format!("{}% / 100%", used.trim_end_matches(".0"))
                },
            );
            println!("{provider}\t{account}\t{window}\t{usage}\t{observed}");
        }
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn doc(at: i64) -> String {
        json!({"observed_at":at,"rows":[{"provider":"codex:a","windowKey":"5h","timestamp":at,"remainingPct":70}]}).to_string()
    }
    #[test]
    fn center_snapshot_is_used_while_fresh_and_names_its_age_when_stale() {
        let now = 1_800_000_000;
        let rows = center_rows(Some(&doc(now - 600)), now, 900, 10_800).unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        let stale = center_rows(Some(&doc(now - 5_400)), now, 900, 10_800).unwrap_err();
        assert_eq!(
            stale,
            StaleQuota("quota_stale: center quota snapshot is 5400s old (limit 900s)".into())
        );
        for bad in [
            None,
            Some("not json"),
            Some("{\"rows\":[]}"),
            Some("{\"observed_at\":1}"),
        ] {
            assert!(center_rows(bad, now, 900, 10_800)
                .unwrap_err()
                .0
                .starts_with("quota_stale:"));
        }
        assert!(center_rows(Some(&doc(now + 5)), now, 900, 10_800).is_err());
    }
    #[test]
    fn a_fresh_envelope_around_old_or_empty_observations_is_stale_quota_with_ages() {
        let now = 1_800_000_000;
        let wrap = |rows: Value| json!({"observed_at":now - 30,"rows":rows}).to_string();
        let old = json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 20_000,"remainingPct":70}]);
        let err = center_rows(Some(&wrap(old.clone())), now, 900, 10_800).unwrap_err();
        assert_eq!(
            err.0,
            "quota_stale: center quota snapshot is 30s old but holds no usable codex observation (newest 20000s old, limit 10800s)"
        );
        let err = center_rows(Some(&wrap(json!([]))), now, 900, 10_800).unwrap_err();
        assert!(err.0.contains("no codex observation"), "{err:?}");
        // The center refuses to publish either, so the worker's file keeps aging instead.
        assert!(publishable(&old, now, 10_800)
            .unwrap_err()
            .contains("newest 20000s old"));
        assert!(publishable(&json!([]), now, 10_800).is_err());
        let fresh =
            json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 60,"remainingPct":70}]);
        assert!(publishable(&fresh, now, 10_800).is_ok());
    }
    #[test]
    fn a_host_without_codex_rows_reads_the_center_file_and_never_gets_null_quota() {
        let now = 1_800_000_000;
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("quota-facts.json");
        let local = Some(json!([]));
        let err = rows_for_host(local.clone(), None, &file, now, 900, 10_800).unwrap_err();
        assert!(err.0.starts_with("quota_stale:"), "{err:?}");
        std::fs::write(&file, doc(now - 60)).unwrap();
        let rows = rows_for_host(local.clone(), None, &file, now, 900, 10_800)
            .unwrap()
            .unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        // Own codex rows (the center) and supplied facts quota both skip the file.
        let own = Some(
            json!([{"provider":"codex:b","windowKey":"5h","timestamp":now,"remainingPct":40}]),
        );
        std::fs::remove_file(&file).unwrap();
        assert_eq!(
            rows_for_host(own.clone(), None, &file, now, 900, 10_800).unwrap(),
            own
        );
        assert_eq!(
            rows_for_host(None, Some(50.0), &file, now, 900, 10_800).unwrap(),
            None
        );
    }
    #[test]
    fn aliases_exhaustion_freshness_and_account_isolation() {
        let now = 1_800_000_000;
        let row = |a: &str, w: &str, at: i64, q: Value| json!({"provider":format!("codex:{a}"),"windowKey":w,"timestamp":at,"remainingPct":q});
        let mut rows = vec![
            row("a", "day", now - 10, json!(90)),
            row("a", "week", now - 10, json!(30)),
            row("b", "day", now - 20, json!(99)),
            row("b", "day", now - 5, json!(2)),
            row("broken", "day", now - 5, json!("bad")),
        ];
        assert_eq!(remaining(&json!(rows), now, 10800), Some(30.0));
        rows[0]["isExhausted"] = json!("true");
        assert_eq!(remaining(&json!(rows), now, 10800), Some(2.0));
        rows[3]["isExhausted"] = json!("1");
        assert_eq!(remaining(&json!(rows), now, 10800), Some(0.0));
        assert_eq!(
            remaining(
                &json!([row("a", "day", now - 10801, json!(99))]),
                now,
                10800
            ),
            None
        );
        assert_eq!(
            remaining(&json!([row("a", "day", now + 1, json!(99))]), now, 10800),
            None
        );
        assert_eq!(remaining(&json!([]), now, 10800), None);
    }
    #[test]
    fn newest_malformed_window_cannot_reuse_older_high_quota() {
        let rows = json!([
            {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:00Z","remaining_percentage":99},
            {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:01Z","remaining_percentage":null},
            {"provider":"codex","connection_id":"b","window_key":"day","created_at":"2026-01-01 00:00:01","remaining_percentage":40}
        ]);
        assert_eq!(remaining(&rows, 1767225602, 10800), Some(40.0));
    }
}
