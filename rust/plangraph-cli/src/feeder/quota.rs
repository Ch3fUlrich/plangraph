use super::adapters::{quota_snapshots, Env};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
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

/// Seconds a quota window lasts, from its key (`daily`, `weekly`, `5h`, `7d`); `None` when unknown.
fn window_seconds(key: &str) -> Option<i64> {
    match key {
        "day" | "daily" => return Some(86_400),
        "week" | "weekly" => return Some(604_800),
        _ => {}
    }
    let (n, unit) = key.split_at(key.len().checked_sub(1)?);
    let n: i64 = n.parse().ok().filter(|n| *n > 0)?;
    Some(
        n * if unit == "h" {
            3_600
        } else if unit == "d" {
            86_400
        } else {
            return None;
        },
    )
}
/// One pool-use sample per window of `target`: the mean used percent over the accounts'
/// newest fresh rows, and the earliest reset still ahead (`pool_use` input of the watchdog;
/// `live`, `waiting` and `admission_slots` are filled by the caller). Windows without a
/// known length, a reset in the future or a fresh row are left out, never guessed.
pub(crate) fn pool_use_samples(rows: &Value, target: &str, now: i64, max_age: i64) -> Vec<Value> {
    let mut latest: BTreeMap<(String, String), (i64, f64, Option<i64>)> = BTreeMap::new();
    for r in rows.as_array().into_iter().flatten() {
        let Some((provider, account)) = identity(r) else {
            continue;
        };
        let (Some(w), Some(at)) = (window(r), observed(r).and_then(stamp)) else {
            continue;
        };
        let Some(used) = remaining_percent(r).map(|left| 100.0 - left) else {
            continue;
        };
        if provider != target || at > now || now - at > max_age {
            continue;
        }
        let reset = field(r, &["next_reset_at", "nextResetAt", "resetAt"]).and_then(stamp);
        let entry = latest
            .entry((w.into(), account.into()))
            .or_insert((at, used, reset));
        if at >= entry.0 {
            *entry = (at, used, reset);
        }
    }
    let mut windows: BTreeMap<String, Vec<(f64, Option<i64>)>> = BTreeMap::new();
    for ((w, _), (_, used, reset)) in latest {
        windows.entry(w).or_default().push((used, reset));
    }
    windows
        .into_iter()
        .filter_map(|(w, accounts)| {
            let length = window_seconds(&w)?;
            let reset = accounts
                .iter()
                .filter_map(|a| a.1)
                .filter(|r| *r > now)
                .min()?;
            let used = accounts.iter().map(|a| a.0).sum::<f64>() / accounts.len() as f64;
            Some(serde_json::json!({"pool":target,"window":w,"now":now,
                "window_start":reset - length,"reset_at":reset,"used_pct":used}))
        })
        .collect()
}
/// `plangraph quota pool [--db PATH]`: print the pool-use samples of the codex pool as JSON.
fn run_pool(env: &Env, args: &[String]) -> i32 {
    let mut effective = env.clone();
    match args {
        [] => {}
        [flag, path] if flag == "--db" && !path.is_empty() => {
            effective.insert("QUOTA_SNAPSHOT_DB".into(), path.clone());
        }
        _ => {
            eprintln!("usage: plangraph quota pool [--db PATH]");
            return 2;
        }
    }
    let rows = std::env::current_dir()
        .ok()
        .and_then(|repo| quota_snapshots(&effective, &repo).ok());
    let Some(rows) = rows else {
        eprintln!("quota unknown");
        return 1;
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    println!(
        "{}",
        Value::Array(pool_use_samples(&rows, "codex", now, 10_800))
    );
    0
}

/// Print the most recent available observation for each provider, account and window.
pub fn run_status(env: &Env, args: &[String]) -> i32 {
    if args.first().is_some_and(|a| a == "pool") {
        return run_pool(env, &args[1..]);
    }
    let mut effective = env.clone();
    match args {
        [] => {}
        [flag, path] if flag == "--db" && !path.is_empty() => {
            effective.insert("QUOTA_SNAPSHOT_DB".into(), path.clone());
        }
        _ => {
            eprintln!("usage: plangraph quota [--db PATH]");
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
    #[test]
    fn pool_use_samples_are_per_window_with_earliest_reset_and_mean_use() {
        let now = 1_800_000_000;
        let row = |a: &str, w: &str, left: i64, reset: i64| {
            json!({"provider":format!("codex:{a}"),
            "window_key":w,"created_at":now - 60,"remaining_percentage":left,"next_reset_at":reset})
        };
        let rows = json!([
            row("a", "weekly", 80, now + 3 * 86_400),
            row("b", "weekly", 60, now + 2 * 86_400),
            row("a", "5h", 100, now + 3_600),
            row("a", "odd", 10, now + 3_600),
            row("a", "daily", 10, now - 5),
            json!({"provider":"claude","window_key":"weekly","created_at":now - 60,
                "remaining_percentage":1,"next_reset_at":now + 100}),
        ]);
        let got = pool_use_samples(&rows, "codex", now, 10_800);
        let by = |w: &str| got.iter().find(|s| s["window"] == w).cloned();
        let weekly = by("weekly").unwrap();
        assert_eq!(weekly["used_pct"], 30.0);
        assert_eq!(weekly["reset_at"], now + 2 * 86_400);
        assert_eq!(weekly["window_start"], now + 2 * 86_400 - 604_800);
        assert_eq!(by("5h").unwrap()["used_pct"], 0.0);
        assert!(by("odd").is_none() && by("daily").is_none() && got.len() == 2);
        assert!(pool_use_samples(&rows, "codex", now + 20_000, 10_800).is_empty());
    }
}
