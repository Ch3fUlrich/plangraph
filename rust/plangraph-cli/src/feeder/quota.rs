use super::adapters::{quota_snapshots, Env};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
pub(super) fn field<'a>(r: &'a Value, names: &[&str]) -> Option<&'a Value> {
    names.iter().find_map(|n| r.get(n).filter(|v| !v.is_null()))
}
pub(super) fn identity(row: &Value) -> Option<(&str, &str)> {
    identity_with(row, false)
}
/// The pool readout's identity: an empty connection id names no account, the provider suffix
/// does. The other readers keep `identity`, which reads an empty id as the account itself.
pub(super) fn pool_identity(row: &Value) -> Option<(&str, &str)> {
    identity_with(row, true)
}
fn identity_with(row: &Value, empty_id_is_absent: bool) -> Option<(&str, &str)> {
    let name = row["provider"].as_str()?;
    let (provider, suffix) = name.split_once(':').unwrap_or((name, ""));
    let account = field(row, &["connection_id", "connectionId"])
        .and_then(Value::as_str)
        .filter(|id| !(empty_id_is_absent && id.is_empty()))
        .unwrap_or(suffix);
    Some((provider, account))
}
pub(super) fn observed(row: &Value) -> Option<&Value> {
    field(row, &["created_at", "createdAt", "timestamp"])
}
pub(super) fn window(row: &Value) -> Option<&str> {
    field(row, &["window_key", "windowKey"]).and_then(Value::as_str)
}
pub(super) fn remaining_percent(row: &Value) -> Option<f64> {
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
/// Latest instant the pool readout reports: nanoseconds fit an i64 (year 2255).
pub(super) const STAMP_MAX_NS: i128 = 9_000_000_000 * 1_000_000_000;
/// `stamp_ns` clamps to this, far inside i128, so arithmetic on any stored value cannot overflow
/// while an absurd instant still ranks before or after every real one.
const STAMP_BOUND_NS: i128 = 1_000_000_000_000_000_000_000_000;
/// Epoch nanoseconds for the pool readout: one instant ranks the same in any form, and
/// observations inside one second still order. A number is read as the decimal the store wrote
/// (a float cannot hold 1800000000.1, its shortest decimal can); the snapshot reader hands
/// numeric times over as that decimal text, since a JSON number is rounded when decoded. A
/// zone-less text is UTC, and `T` or a space may separate date and time. Every parseable
/// instant has a rank, clamped to +-`STAMP_BOUND_NS`; whether it is a usable reading (not
/// future, not before 1970, not after `STAMP_MAX_NS`) is the caller's decision, taken after the
/// newest row is chosen.
pub(super) fn stamp_ns(v: &Value) -> Option<i128> {
    use chrono::{DateTime, NaiveDateTime, Utc};
    let text = v.as_f64().map(|n| n.to_string());
    let s = text.as_deref().or_else(|| v.as_str().map(str::trim))?;
    let ns = if s.bytes().all(|b| b"0123456789+-.eE".contains(&b)) && s.parse::<f64>().is_ok() {
        decimal_ns(s)?
    } else {
        let ns = |t: DateTime<Utc>| {
            i128::from(t.timestamp()) * 1_000_000_000 + i128::from(t.timestamp_subsec_nanos())
        };
        DateTime::parse_from_rfc3339(s)
            .or_else(|_| DateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f%#z"))
            .map(|t| ns(t.to_utc()))
            .ok()
            .or_else(|| {
                ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f"]
                    .iter()
                    .find_map(|f| NaiveDateTime::parse_from_str(s, f).ok())
                    .map(|t| ns(t.and_utc()))
            })?
    };
    Some(ns.clamp(-STAMP_BOUND_NS, STAMP_BOUND_NS))
}
/// A decimal number text (checked by the caller to be a finite float) as nanoseconds: seconds,
/// or milliseconds once the value passes 1e11 as in the legacy reader. The digits and the
/// exponent are scaled as text, never routed through a float (which keeps 15 to 17 digits), and
/// the fraction is cut at the nanosecond. The float is read only for the size of the value.
fn decimal_ns(text: &str) -> Option<i128> {
    let n = text.parse::<f64>().ok().filter(|n| n.is_finite())?;
    if n.abs() > 1e15 {
        return Some(if n < 0.0 {
            -STAMP_BOUND_NS
        } else {
            STAMP_BOUND_NS
        });
    }
    let places: i64 = if n > 1e11 { 6 } else { 9 };
    let unsigned = text.strip_prefix(['+', '-']).unwrap_or(text);
    let (mantissa, exponent) = unsigned.split_once(['e', 'E']).unwrap_or((unsigned, ""));
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    // The digits and how many of them lie before the decimal point, leading zeros dropped.
    let all = format!("{whole}{fraction}");
    let digits = all.trim_start_matches('0');
    if digits.is_empty() {
        return Some(0); // a zero mantissa is zero whatever its exponent says
    }
    // An exponent past i64 is a float underflow when negative (the size check refused the rest).
    let exponent = match exponent.parse::<i64>() {
        _ if exponent.is_empty() => 0,
        Ok(exponent) => exponent,
        Err(_) if exponent.starts_with('-') => return Some(0),
        Err(_) => return None,
    };
    let point = (whole.len() as i64)
        .saturating_add(exponent)
        .saturating_sub((all.len() - digits.len()) as i64);
    // The first `point + places` digits are the value in units of 10^-places of the unit. The
    // size check above bounds `point` near 16, so more than 25 digits cannot be a real value.
    let keep = point.saturating_add(places).max(0);
    if keep > 38 {
        return None;
    }
    let kept: String = digits
        .chars()
        .chain(std::iter::repeat('0'))
        .take(keep as usize)
        .collect();
    let value = if kept.is_empty() {
        0
    } else {
        kept.parse::<i128>().ok()?
    };
    Some(if text.starts_with('-') { -value } else { value })
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

/// Print the most recent available observation for each provider, account and window;
/// `pool` prints the per-window readout of the codex pool instead.
pub fn run_status(env: &Env, args: &[String]) -> i32 {
    if args.first().is_some_and(|a| a == "pool") {
        return super::pool::run_pool(env, &args[1..]);
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
    fn stamp_ns_reads_one_instant_alike_and_ranks_every_parseable_instant() {
        let iso = stamp_ns(&json!("2027-01-15T08:00:00.250000000")).unwrap();
        assert_eq!(stamp_ns(&json!(1_799_999_999.25 + 1.0)), Some(iso)); // seconds, a float fraction
        assert_eq!(stamp_ns(&json!(1_799_999_999_250.0 + 1_000.0)), Some(iso)); // milliseconds
        assert_eq!(stamp_ns(&json!("2027-01-15 08:00:00.25+00:00")), Some(iso));
        assert_eq!(stamp_ns(&json!("2027-01-15 09:00:00.25 +01:00")), Some(iso));
        // A fraction a float cannot hold keeps its decimal reading.
        assert_eq!(
            stamp_ns(&json!(1_800_000_000.1)),
            stamp_ns(&json!("2027-01-15T08:00:00.1"))
        );
        // Absurd instants rank at the ends, bounded, never lost.
        let (low, high) = (
            stamp_ns(&json!(-1e100)).unwrap(),
            stamp_ns(&json!(1e300)).unwrap(),
        );
        let future = stamp_ns(&json!("9999-12-31T00:00:00Z")).unwrap();
        assert!(low < stamp_ns(&json!(-1.0)).unwrap() && high > future && future > STAMP_MAX_NS);
        assert!(stamp_ns(&json!(-1.0)).unwrap() < 0 && iso < STAMP_MAX_NS);
        assert!(low > i128::MIN / 2 && high < i128::MAX / 2);
        assert_eq!(stamp_ns(&json!("not a time")), None);
        // The snapshot reader writes a numeric time as its decimal text; a JSON number could
        // not keep this fraction (default decoding moves it 200 ns).
        let ms = stamp_ns(&json!("1795581490000.0015")).unwrap();
        assert_eq!(ms, 1_795_581_490_000_001_500);
        assert_eq!(stamp_ns(&json!("1795581490.0000015")), Some(ms));
        assert_eq!(stamp_ns(&json!("1.5e-07")), Some(150));
        // Exponent text keeps every digit: a float would drop the last nanosecond here.
        assert_eq!(
            stamp_ns(&json!("1.795581490000001501e12")),
            Some(1_795_581_490_000_001_501)
        );
        assert_eq!(
            stamp_ns(&json!("17955814900000015.01e-4")),
            Some(1_795_581_490_000_001_501)
        );
        assert_eq!(stamp_ns(&json!("0.0000000000001e13")), Some(1_000_000_000));
        assert_eq!(stamp_ns(&json!("-1.5e0")), Some(-1_500_000_000));
        // An enormous exponent costs nothing: zero stays zero, a vanishing value is zero.
        for text in [
            "0e2147483647",
            "0e2147483630",
            "-0e2147483648",
            "0e99999999999999999999",
        ] {
            assert_eq!(stamp_ns(&json!(text)), Some(0), "{text}");
        }
        for text in ["1e-2147483648", "1e-99999999999", "1e-99999999999999999999"] {
            assert_eq!(stamp_ns(&json!(text)), Some(0), "{text}");
        }
        assert_eq!(
            stamp_ns(&json!("+1800000000.5")),
            stamp_ns(&json!(1_800_000_000.5))
        );
        for word in ["inf", "NaN", "e", "-", "."] {
            assert_eq!(stamp_ns(&json!(word)), None, "{word}");
        }
    }
    #[test]
    fn the_legacy_readers_keep_an_empty_connection_id_apart_from_the_suffix_account() {
        let now = 1_800_000_000;
        let rows = json!([
            {"provider":"codex:a","connection_id":"","window_key":"day","created_at":now - 10,"remaining_percentage":80},
            {"provider":"codex","connection_id":"a","window_key":"week","created_at":now - 10,"remaining_percentage":10},
        ]);
        // Two accounts (the unnamed one and `a`): the pool takes the better, not the merged minimum.
        assert_eq!(remaining(&rows, now, 10_800), Some(80.0));
        assert_eq!(identity(&rows[0]), Some(("codex", "")));
        assert_eq!(pool_identity(&rows[0]), Some(("codex", "a")));
    }
}
