use super::adapters::{quota_snapshots, Env};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
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

fn has_codex_rows(rows: &Value) -> bool {
    rows.as_array().is_some_and(|rows| {
        rows.iter()
            .any(|row| identity(row).is_some_and(|(provider, _)| provider == "codex"))
    })
}

#[derive(Debug, PartialEq, Eq)]
pub(super) struct StaleQuota(pub String);

fn newest_codex_age(rows: &Value, now: i64) -> Option<i64> {
    rows.as_array()?
        .iter()
        .filter(|row| identity(row).is_some_and(|(provider, _)| provider == "codex"))
        .filter_map(|row| observed(row).and_then(stamp))
        .max()
        .map(|at| now.saturating_sub(at))
}

/// Read the center envelope under the worker's own envelope and observation age limits.
fn center_rows(
    text: Option<&str>,
    now: i64,
    max_age: i64,
    observation_max_age: i64,
) -> Result<Value, StaleQuota> {
    let doc: Option<Value> = text.and_then(|text| serde_json::from_str(text).ok());
    let observed_at = doc.as_ref().and_then(|doc| doc["observed_at"].as_i64());
    let rows = doc.as_ref().map_or(&Value::Null, |doc| &doc["rows"]);
    let snapshot_age = observed_at.map(|at| now.saturating_sub(at));
    let problem = if doc.is_none() {
        "no readable center quota snapshot"
    } else if snapshot_age.is_none() {
        "center quota snapshot has no valid observed_at"
    } else if snapshot_age.is_some_and(|age| !(0..=max_age).contains(&age)) {
        "center quota snapshot is outside its freshness window"
    } else if remaining(rows, now, observation_max_age).is_none() {
        "center quota snapshot has no usable codex observation"
    } else {
        return Ok(rows.clone());
    };
    let age_text = |age: Option<i64>| age.map_or_else(|| "unknown".into(), |age| format!("{age}s"));
    Err(StaleQuota(format!(
        "quota_stale: {problem}; snapshot age {} (limit {max_age}s); codex observation age {} (limit {observation_max_age}s)",
        age_text(snapshot_age),
        age_text(newest_codex_age(rows, now)),
    )))
}

/// A host with its own codex observations keeps its local reader. Otherwise live
/// admission reads the center snapshot. A quota supplied in explicit facts keeps
/// the existing fallback for an unavailable local reader.
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
mod tests;
