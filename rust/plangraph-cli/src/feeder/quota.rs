use super::adapters::{quota_snapshots, quota_snapshots_by_observation, Env};
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
    stamp_secs(v).map(|t| t as i64)
}
/// Epoch seconds with the fraction kept, so two observations in one second still order.
fn stamp_secs(v: &Value) -> Option<f64> {
    if let Some(n) = v.as_f64() {
        return n
            .is_finite()
            .then_some(if n > 1e11 { n / 1000.0 } else { n });
    }
    let s = v.as_str()?;
    let secs = |t: chrono::DateTime<chrono::Utc>| {
        t.timestamp() as f64 + f64::from(t.timestamp_subsec_nanos()) / 1e9
    };
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|t| secs(t.to_utc()))
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .map(|t| secs(t.and_utc()))
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
    let (n, unit) = (key.strip_suffix('h'), 3_600);
    let (n, unit) = match n {
        Some(n) => (n, unit),
        None => (key.strip_suffix('d')?, 86_400),
    };
    n.parse::<i64>().ok().filter(|n| *n > 0).map(|n| n * unit)
}
/// A whole number stays an integer in the JSON output, a fraction stays a float.
fn num(x: f64) -> Value {
    if x.fract() == 0.0 && x.abs() < 9e15 {
        Value::from(x as i64)
    } else {
        Value::from(x)
    }
}
struct Contributor<'a> {
    account: &'a str,
    used: f64,
    at: f64,
    reset: f64,
}
#[derive(Default)]
struct WindowSeen<'a> {
    observed: usize,
    unidentified: usize,
    cohort: Vec<Contributor<'a>>,
}
fn mean(values: impl Iterator<Item = f64>) -> Option<f64> {
    let (sum, n) = values.fold((0.0, 0usize), |(s, n), v| (s + v, n + 1));
    (n > 0).then(|| sum / n as f64)
}
/// One sample per window the `target` pool has rows for. The newest row of each canonical
/// (provider, account, window) wins first (`codex:a` and `codex` with `connection_id=a` are one
/// account); only then are age, usage and reset checked, so an invalid newest row never lets an
/// older one speak for the account. `used_pct` and `pace_pct` average the same cohort, the
/// observed accounts whose newest row is fresh, valid and still inside its window; each
/// account's pace is its own elapsed fraction at its own observation time. The snapshot source
/// has no account roster, so `coverage.full_pool` stays "unknown" and the averages describe the
/// cohort only. Rows with no account identity are counted apart and never averaged.
pub(crate) fn pool_use_samples(rows: &Value, target: &str, now: i64, max_age: i64) -> Vec<Value> {
    let now = now as f64;
    let mut latest: BTreeMap<(&str, &str), (Option<f64>, &Value)> = BTreeMap::new();
    for r in rows.as_array().into_iter().flatten() {
        let Some((provider, account)) = identity(r) else {
            continue;
        };
        let Some(w) = window(r) else { continue };
        if provider != target {
            continue;
        }
        let at = observed(r).and_then(stamp_secs);
        let entry = latest.entry((w, account)).or_insert((at, r));
        if at.is_some() && at >= entry.0 {
            *entry = (at, r);
        }
    }
    let mut windows: BTreeMap<&str, WindowSeen> = BTreeMap::new();
    for ((w, account), (at, row)) in latest {
        let seen = windows.entry(w).or_default();
        if account.is_empty() {
            seen.unidentified += 1;
            continue;
        }
        seen.observed += 1;
        let used = remaining_percent(row).map(|left| 100.0 - left);
        let reset = field(row, &["next_reset_at", "nextResetAt", "resetAt"]).and_then(stamp_secs);
        if let (Some(at), Some(used), Some(reset)) = (at, used, reset) {
            if at <= now && now - at <= max_age as f64 && reset > now {
                seen.cohort.push(Contributor {
                    account,
                    used,
                    at,
                    reset,
                });
            }
        }
    }
    windows
        .into_iter()
        .map(|(w, seen)| {
            let length = window_seconds(w).map(|l| l as f64);
            let elapsed = |c: &Contributor| {
                length.map(|l| ((c.at - (c.reset - l)) / l).clamp(0.0, 1.0) * 100.0)
            };
            let reset = seen.cohort.iter().map(|c| c.reset).reduce(f64::min);
            let accounts: Vec<Value> = seen
                .cohort
                .iter()
                .map(|c| {
                    serde_json::json!({"account":c.account,"used_pct":c.used,"observed_at":num(c.at),
                        "reset_at":num(c.reset),"elapsed_pct":elapsed(c)})
                })
                .collect();
            serde_json::json!({"pool":target,"window":w,"now":now as i64,
                "used_pct":mean(seen.cohort.iter().map(|c| c.used)),
                "pace_pct":mean(seen.cohort.iter().filter_map(elapsed)),
                "observed_at":seen.cohort.iter().map(|c| c.at).reduce(f64::max).map(num),
                "reset_at":reset.map(num),
                "window_start":reset.zip(length).map(|(r, l)| num(r - l)),
                "accounts":accounts,
                "coverage":{"observed_accounts":seen.observed,"usable_accounts":seen.cohort.len(),
                    "unidentified":seen.unidentified,"full_pool":"unknown"}})
        })
        .collect()
}
/// The `plangraph quota pool` document: samples per window, or an explicit unknown status.
fn pool_report(rows: Option<&Value>, target: &str, now: i64, max_age: i64) -> Value {
    let windows = rows.map_or_else(Vec::new, |r| pool_use_samples(r, target, now, max_age));
    let usable = windows
        .iter()
        .any(|w| w["coverage"]["usable_accounts"].as_u64().unwrap_or(0) > 0);
    let reason = match (rows.is_some(), windows.is_empty(), usable) {
        (false, ..) => Some("snapshots unreadable"),
        (_, true, _) => Some("no identifiable windows"),
        (_, _, false) => Some("no usable observations"),
        _ => None,
    };
    serde_json::json!({"pool":target,"status":if usable {"ok"} else {"unknown"},"reason":reason,
        "windows":windows})
}
/// `plangraph quota pool [--db PATH]`: print the pool-use document of the codex pool as JSON.
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
        .and_then(|repo| quota_snapshots_by_observation(&effective, &repo).ok());
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    println!("{}", pool_report(rows.as_ref(), "codex", now, 10_800));
    0
}

/// Print the most recent available observation for each provider, account and window;
/// `pool` prints the per-window readout of the codex pool instead.
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
        // A window of unknown length keeps its usage and has no pace; an ended window has no usage.
        let odd = by("odd").unwrap();
        assert_eq!(
            (odd["used_pct"].as_f64(), odd["pace_pct"].is_null()),
            (Some(90.0), true)
        );
        assert!(by("daily").unwrap()["used_pct"].is_null() && got.len() == 4);
        // Nothing fresh: every window is still listed, with unknown usage and zero usable accounts.
        let stale = pool_use_samples(&rows, "codex", now + 20_000, 10_800);
        assert!(stale.iter().all(|s| s["used_pct"].is_null()
            && s["coverage"]["usable_accounts"] == 0
            && s["coverage"]["observed_accounts"].as_u64() > Some(0)));
        // An expired 100 %-used account never lifts the sample of the account still in its window.
        let mixed = json!([
            row("a", "weekly", 0, now - 5),
            row("b", "weekly", 80, now + 86_400)
        ]);
        assert_eq!(
            pool_use_samples(&mixed, "codex", now, 10_800)[0]["used_pct"],
            20.0
        );
    }
    #[test]
    fn newest_row_wins_before_validity_and_anonymous_rows_never_average() {
        let now = 1_800_000_000;
        let rows = json!([
            {"provider":"codex:a","window_key":"weekly","created_at":now - 120,"remaining_percentage":50,"next_reset_at":now + 100},
            {"provider":"codex","connection_id":"a","window_key":"weekly","created_at":now - 60,"remaining_percentage":"bad","next_reset_at":now + 100},
            {"provider":"codex","window_key":"weekly","created_at":now - 60,"remaining_percentage":0,"next_reset_at":now + 100},
        ]);
        let w = &pool_use_samples(&rows, "codex", now, 10_800)[0];
        assert!(w["used_pct"].is_null() && w["accounts"] == json!([]));
        assert_eq!(
            w["coverage"],
            json!({"observed_accounts":1,"usable_accounts":0,"unidentified":1,"full_pool":"unknown"})
        );
    }
    #[test]
    fn a_response_without_window_identity_is_an_explicit_unknown() {
        let api = json!([{"provider":"codex:a1","remainingPct":0}]);
        let got = pool_report(Some(&api), "codex", 1_800_000_000, 10_800);
        assert_eq!(
            (got["status"].as_str(), got["reason"].as_str()),
            (Some("unknown"), Some("no identifiable windows"))
        );
        assert_eq!(
            pool_report(None, "codex", 1, 1)["reason"],
            "snapshots unreadable"
        );
    }
}
