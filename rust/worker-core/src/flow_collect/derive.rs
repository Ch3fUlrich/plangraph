//! The scalar fields of the flow snapshot, each derived from one reading.
use super::{Item, Readings, SERIES_MAX_AGE_S};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

const DAY: f64 = 86_400.0;
const CI_WINDOW: f64 = 7.0 * DAY;
const CLOSING: [&str; 3] = ["review_passed", "closed_on_decision", "answered"];
type Fields = Result<Map<String, Value>, String>;

pub fn parse_time(text: &str) -> Result<f64, String> {
    chrono::DateTime::parse_from_rfc3339(text)
        .map(|time| time.timestamp_millis() as f64 / 1000.0)
        .map_err(|_| format!("unreadable timestamp {text:?}"))
}

fn fields(pairs: &[(&str, Value)]) -> Fields {
    Ok(pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect())
}

fn missing<T>(reading: &Option<T>, name: &str) -> Result<(), String> {
    if reading.is_none() {
        return Err(format!("{name} reading failed"));
    }
    Ok(())
}

pub fn usage(r: &Readings) -> Fields {
    let usage = r.usage.as_ref().ok_or("usage reading failed")?;
    fields(&[
        ("pace", json!(usage.weighted)),
        ("budget", json!(usage.budget_per_day)),
    ])
}

/// Failures are failure, timed_out and startup_failure; the last two are infrastructure.
pub fn ci(r: &Readings) -> Fields {
    let runs = r.ci.as_ref().ok_or("ci reading failed")?;
    let (mut total, mut failed, mut infra) = (0u64, 0u64, 0u64);
    for run in runs {
        let age = r.now - parse_time(&run.created)?;
        if !(0.0..=CI_WINDOW).contains(&age) {
            continue;
        }
        match run.conclusion.as_str() {
            "success" => total += 1,
            "failure" => (total, failed) = (total + 1, failed + 1),
            "timed_out" | "startup_failure" => {
                (total, failed, infra) = (total + 1, failed + 1, infra + 1)
            }
            _ => {}
        }
    }
    fields(&[
        ("ci_total", json!(total)),
        ("ci_failed", json!(failed)),
        ("infra_failed", json!(infra)),
    ])
}

pub fn open(item: &Item) -> bool {
    !matches!(item.state.as_str(), "done" | "closed" | "cancelled")
}

fn updated(item: &Item) -> Result<f64, String> {
    parse_time(
        item.updated_at
            .as_deref()
            .ok_or_else(|| format!("{} has no updated_at", item.key))?,
    )
}

/// A review round is one more than the changes requested since the item's latest claim,
/// so a fresh claim of the same key does not inherit the rounds of an earlier one.
pub fn review(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let events = r
        .activity
        .as_ref()
        .ok_or("keeper activity reading failed")?;
    let (mut age, mut rounds) = (0.0f64, 0u64);
    for item in items.iter().filter(|item| item.state == "in_review") {
        age = age.max((r.now - updated(item)?).max(0.0) / 3600.0);
        let mine = || events.iter().filter(|e| e.key == item.key);
        let mut claimed = None;
        for event in mine().filter(|e| e.kind == "claimed") {
            let at = parse_time(&event.ts)?;
            claimed = Some(claimed.map_or(at, |c: f64| c.max(at)));
        }
        let mut changes = 0u64;
        for event in mine().filter(|e| e.kind == "changes_requested") {
            changes += u64::from(
                claimed.is_none_or(|since| parse_time(&event.ts).is_ok_and(|t| t >= since)),
            );
        }
        rounds = rounds.max(changes + 1);
    }
    fields(&[
        ("review_age_hours", json!(age)),
        ("review_rounds", json!(rounds)),
    ])
}

/// The critical-path stall rule: the longest keeper silence among the claimed and in-review
/// items among the open dependencies of the stall service's target. Unstarted open items are
/// waiting, not stalled; a lease is not activity.
pub fn critical(r: &Readings) -> Fields {
    let target = r
        .critical_target
        .as_ref()
        .ok_or("critical target reading failed")?;
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let index: BTreeMap<&str, &Item> = items.iter().map(|i| (i.key.as_str(), i)).collect();
    let root = index
        .get(target.as_str())
        .ok_or_else(|| format!("target absent from keeper snapshot: {target}"))?;
    let mut seen = BTreeSet::from([target.clone()]);
    let mut pending = super::stuck::deps(root);
    let mut idle = 0.0f64;
    while let Some(key) = pending.pop() {
        if !seen.insert(key.clone()) {
            continue;
        }
        let item = index
            .get(key.as_str())
            .ok_or_else(|| format!("dependency absent from keeper snapshot: {key}"))?;
        if !open(item) {
            continue;
        }
        pending.extend(super::stuck::deps(item));
        if matches!(item.state.as_str(), "claimed" | "in_review") {
            let at = item
                .last_activity_at
                .as_deref()
                .ok_or_else(|| format!("{} has no keeper last_activity", item.key))?;
            idle = idle.max((r.now - parse_time(at)?).max(0.0) / 3600.0);
        }
    }
    fields(&[("critical_idle_hours", json!(idle))])
}

/// Current, reachable samples of the configured hosts. A configured host without one is a gap:
/// an empty or stale series proves neither "no runs" nor "idle worker".
fn current<'a>(
    r: &'a Readings,
    hosts: &[String],
    need_reachable: bool,
) -> Result<Vec<&'a super::Sample>, String> {
    let series = r.series.as_ref().ok_or("fleet series reading failed")?;
    let mut rows = Vec::new();
    for host in hosts {
        let newest = series
            .iter()
            .filter(|s| &s.host == host)
            .max_by(|a, b| a.ts.total_cmp(&b.ts))
            .ok_or_else(|| format!("no fleet sample for {host}"))?;
        if r.now - newest.ts > SERIES_MAX_AGE_S {
            return Err(format!("fleet sample for {host} is stale"));
        }
        if need_reachable && !newest.reachable {
            return Err(format!("{host} was unreachable at its last sample"));
        }
        rows.push(newest);
    }
    Ok(rows)
}

pub fn placement(r: &Readings) -> Fields {
    if r.coding_hosts.is_empty() || r.worker_hosts.is_empty() {
        return Err("coding and worker hosts are not configured".into());
    }
    let runs = current(r, &r.coding_hosts, true)?
        .iter()
        .map(|s| s.runs)
        .sum::<u64>();
    // An unreachable worker is not idle: it cannot take the work either.
    let idle = current(r, &r.worker_hosts, false)?
        .iter()
        .any(|s| s.reachable && s.runs == 0 && s.ready > 0);
    fields(&[
        ("coding_vm_runs", json!(runs)),
        ("worker_idle", json!(idle)),
    ])
}

pub fn drift(r: &Readings) -> Fields {
    let hosts = r.hosts.as_ref().ok_or("host code reading failed")?;
    let units = r.units.as_ref().ok_or("unit checksum reading failed")?;
    let code = hosts
        .iter()
        .any(|h| h.checkout_head != h.origin_main_head && h.behind_commits > 0);
    let unit = units
        .iter()
        .any(|u| u.deployed_sha.as_deref() != Some(u.expected_sha.as_str()));
    fields(&[("host_drift", json!(code || unit))])
}

pub fn closes(r: &Readings) -> Fields {
    let events = r
        .activity
        .as_ref()
        .ok_or("keeper activity reading failed")?;
    let (mut recent, mut previous) = (0u64, 0u64);
    for event in events.iter().filter(|e| CLOSING.contains(&e.kind.as_str())) {
        match r.now - parse_time(&event.ts)? {
            age if (0.0..DAY).contains(&age) => recent += 1,
            age if (DAY..2.0 * DAY).contains(&age) => previous += 1,
            _ => {}
        }
    }
    fields(&[
        ("closes_recent", json!(recent)),
        ("closes_previous", json!(previous)),
    ])
}

pub fn hosts(r: &Readings) -> Fields {
    missing(&r.hosts, "host code")?;
    let rows: Vec<Value> = r
        .hosts
        .iter()
        .flatten()
        .map(|h| {
            json!({"host":h.host,"tmp_used_percent":h.tmp_used_percent,
               "root_used_percent":h.root_used_percent,"work_used_percent":h.work_used_percent,
               "checkout_head":h.checkout_head,"origin_main_head":h.origin_main_head,
               "behind_commits":h.behind_commits,"age_hours":h.age_hours})
        })
        .collect();
    fields(&[("hosts", json!(rows))])
}
