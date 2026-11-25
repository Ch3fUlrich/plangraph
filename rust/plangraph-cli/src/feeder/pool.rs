//! `plangraph quota pool`: the per-window readout of one provider pool from quota snapshots.
use super::adapters::{quota_snapshot_history, Env};
use super::quota::{field, identity, observed, remaining_percent, stamp_ns, window};
use serde_json::Value;
use std::collections::BTreeMap;

const NANOS: f64 = 1e9;
/// A reading older than this is not a current observation (hourly store default: 3 hours).
const MAX_AGE_S: i64 = 10_800;

/// Seconds a quota window lasts, from its key (`daily`, `weekly`, `5h`, `7d`); `None` when unknown.
fn window_seconds(key: &str) -> Option<i64> {
    match key {
        "day" | "daily" => return Some(86_400),
        "week" | "weekly" => return Some(604_800),
        _ => {}
    }
    let (n, unit) = match key.strip_suffix('h') {
        Some(n) => (n, 3_600),
        None => (key.strip_suffix('d')?, 86_400),
    };
    n.parse::<i64>().ok().filter(|n| *n > 0).map(|n| n * unit)
}
fn seconds(ns: i128) -> f64 {
    ns as f64 / NANOS
}
/// A whole number stays an integer in the JSON output, a fraction stays a float.
fn num(x: f64) -> Value {
    if x.fract() == 0.0 && x.abs() < 9e15 {
        Value::from(x as i64)
    } else {
        Value::from(x)
    }
}
fn mean(values: impl Iterator<Item = f64>) -> Option<f64> {
    let (sum, n) = values.fold((0.0, 0usize), |(s, n), v| (s + v, n + 1));
    (n > 0).then(|| sum / n as f64)
}
/// An observed account whose newest row is fresh, valid and inside its window (nanoseconds).
struct Contributor<'a> {
    account: &'a str,
    used: f64,
    at: i128,
    reset: i128,
}
#[derive(Default)]
struct WindowSeen<'a> {
    observed: usize,
    unidentified: usize,
    cohort: Vec<Contributor<'a>>,
}

/// One sample per window the `target` pool has rows for. The newest row of each canonical
/// (provider, account, window) wins first (`codex:a` and `codex` with `connection_id=a` are one
/// account); only then are age, usage, reset and window start checked, so an invalid newest row
/// never lets an older one speak for the account. `used_pct` and `pace_pct` average the same
/// cohort: the observed accounts whose newest row is fresh, valid, inside its window and made
/// after that window began. Each account's pace is its own elapsed fraction at its own
/// observation time. The snapshot source has no account roster, so `coverage.full_pool` stays
/// "unknown" and the averages describe the cohort only. Rows with no account identity are
/// counted apart and never averaged. `now`, observations and resets share one nanosecond clock.
pub(crate) fn pool_use_samples(rows: &Value, target: &str, now: f64, max_age: i64) -> Vec<Value> {
    let now_ns = (now * NANOS).round() as i128;
    let mut latest: BTreeMap<(&str, &str), (Option<i128>, &Value)> = BTreeMap::new();
    for r in rows.as_array().into_iter().flatten() {
        let Some((provider, account)) = identity(r) else {
            continue;
        };
        let Some(w) = window(r) else { continue };
        if provider != target {
            continue;
        }
        let at = observed(r).and_then(stamp_ns);
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
        let reset = field(row, &["next_reset_at", "nextResetAt", "resetAt"]).and_then(stamp_ns);
        let length_ns = window_seconds(w).map(|l| l as i128 * NANOS as i128);
        if let (Some(at), Some(used), Some(reset)) = (at, used, reset) {
            let begun = length_ns.is_none_or(|l| at >= reset - l);
            if at <= now_ns
                && now_ns - at <= max_age as i128 * NANOS as i128
                && reset > now_ns
                && begun
            {
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
                length
                    .map(|l| ((seconds(c.at) - (seconds(c.reset) - l)) / l).clamp(0.0, 1.0) * 100.0)
            };
            let reset = seen.cohort.iter().map(|c| c.reset).min().map(seconds);
            let accounts: Vec<Value> = seen
                .cohort
                .iter()
                .map(|c| {
                    serde_json::json!({"account":c.account,"used_pct":c.used,
                        "observed_at":num(seconds(c.at)),"reset_at":num(seconds(c.reset)),
                        "elapsed_pct":elapsed(c)})
                })
                .collect();
            serde_json::json!({"pool":target,"window":w,"now":num(now),
                "used_pct":mean(seen.cohort.iter().map(|c| c.used)),
                "pace_pct":mean(seen.cohort.iter().filter_map(elapsed)),
                "observed_at":seen.cohort.iter().map(|c| c.at).max().map(|at| num(seconds(at))),
                "reset_at":reset.map(num),
                "window_start":reset.zip(length).map(|(r, l)| num(r - l)),
                "accounts":accounts,
                "coverage":{"observed_accounts":seen.observed,"usable_accounts":seen.cohort.len(),
                    "unidentified":seen.unidentified,"full_pool":"unknown"}})
        })
        .collect()
}
/// The `plangraph quota pool` document: samples per window, or an explicit unknown status.
fn pool_report(rows: Option<&Value>, target: &str, now: f64, max_age: i64) -> Value {
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
pub(super) fn run_pool(env: &Env, args: &[String]) -> i32 {
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
        .and_then(|repo| quota_snapshot_history(&effective, &repo).ok());
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    println!("{}", pool_report(rows.as_ref(), "codex", now, MAX_AGE_S));
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn pool_use_samples_are_per_window_with_earliest_reset_and_mean_use() {
        let now = 1_800_000_000_i64;
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
        let got = pool_use_samples(&rows, "codex", now as f64, 10_800);
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
        let stale = pool_use_samples(&rows, "codex", (now + 20_000) as f64, 10_800);
        assert!(stale.iter().all(|s| s["used_pct"].is_null()
            && s["coverage"]["usable_accounts"] == 0
            && s["coverage"]["observed_accounts"].as_u64() > Some(0)));
        // An expired 100 %-used account never lifts the sample of the account still in its window.
        let mixed = json!([
            row("a", "weekly", 0, now - 5),
            row("b", "weekly", 80, now + 86_400)
        ]);
        assert_eq!(
            pool_use_samples(&mixed, "codex", now as f64, 10_800)[0]["used_pct"],
            20.0
        );
    }
    #[test]
    fn newest_row_wins_before_validity_and_anonymous_rows_never_average() {
        let now = 1_800_000_000_i64;
        let rows = json!([
            {"provider":"codex:a","window_key":"weekly","created_at":now - 120,"remaining_percentage":50,"next_reset_at":now + 100},
            {"provider":"codex","connection_id":"a","window_key":"weekly","created_at":now - 60,"remaining_percentage":"bad","next_reset_at":now + 100},
            {"provider":"codex","window_key":"weekly","created_at":now - 60,"remaining_percentage":0,"next_reset_at":now + 100},
        ]);
        let w = &pool_use_samples(&rows, "codex", now as f64, 10_800)[0];
        assert!(w["used_pct"].is_null() && w["accounts"] == json!([]));
        assert_eq!(
            w["coverage"],
            json!({"observed_accounts":1,"usable_accounts":0,"unidentified":1,"full_pool":"unknown"})
        );
    }
    #[test]
    fn an_empty_connection_id_keeps_the_provider_suffix_identity() {
        let now = 1_800_000_000_i64;
        let rows = json!([{"provider":"codex:a","connection_id":"","window_key":"weekly",
            "created_at":now - 60,"remaining_percentage":70,"next_reset_at":now + 86_400}]);
        let w = &pool_use_samples(&rows, "codex", now as f64, 10_800)[0];
        assert_eq!(w["accounts"][0]["account"], "a");
        assert_eq!(w["coverage"]["unidentified"], 0);
    }
    #[test]
    fn the_clock_keeps_the_fraction_observations_and_resets_keep() {
        let (second, half) = (1_800_000_000_i64, 1_800_000_000.5_f64);
        let row = |at: f64, reset: f64| {
            json!([{"provider":"codex:a","window_key":"5h","created_at":at,
                "remaining_percentage":60,"next_reset_at":reset}])
        };
        // Taken earlier in the current second: not a future observation.
        let early = pool_use_samples(
            &row(second as f64 + 0.25, half + 3_600.0),
            "codex",
            half,
            10_800,
        );
        assert_eq!(early[0]["coverage"]["usable_accounts"], 1, "{early:?}");
        // A reset a quarter second ago has passed, though its whole second has not ended.
        let ended = pool_use_samples(&row(half - 60.0, half - 0.25), "codex", half, 10_800);
        assert_eq!(ended[0]["coverage"]["usable_accounts"], 0, "{ended:?}");
    }
    #[test]
    fn an_observation_before_its_window_began_is_not_averaged() {
        let now = 1_800_000_000_i64;
        let rows = json!([
            {"provider":"codex:a","window_key":"weekly","created_at":now - 60,
                "remaining_percentage":10,"next_reset_at":now - 60 + 604_800 + 3_600},
            {"provider":"codex:b","window_key":"weekly","created_at":now - 60,
                "remaining_percentage":60,"next_reset_at":now + 86_400},
        ]);
        let w = &pool_use_samples(&rows, "codex", now as f64, 10_800)[0];
        assert_eq!(w["used_pct"], 40.0);
        assert_eq!(w["coverage"]["usable_accounts"], 1);
    }
    #[test]
    fn a_response_without_window_identity_is_an_explicit_unknown() {
        let api = json!([{"provider":"codex:a1","remainingPct":0}]);
        let got = pool_report(Some(&api), "codex", 1_800_000_000.0, 10_800);
        assert_eq!(
            (got["status"].as_str(), got["reason"].as_str()),
            (Some("unknown"), Some("no identifiable windows"))
        );
        assert_eq!(
            pool_report(None, "codex", 1.0, 1)["reason"],
            "snapshots unreadable"
        );
    }
}
