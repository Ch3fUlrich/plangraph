//! The scalar fields of the flow snapshot, each derived from one reading.
use super::{Item, Readings};
use serde_json::{json, Map, Value};

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

/// A review round is one more than the count of requested changes on that item.
pub fn review(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let events = r
        .activity
        .as_ref()
        .ok_or("keeper activity reading failed")?;
    let (mut age, mut rounds) = (0.0f64, 0u64);
    for item in items.iter().filter(|item| item.state == "in_review") {
        age = age.max((r.now - updated(item)?).max(0.0) / 3600.0);
        let changes = events
            .iter()
            .filter(|e| e.key == item.key && e.kind == "changes_requested")
            .count() as u64;
        rounds = rounds.max(changes + 1);
    }
    fields(&[
        ("review_age_hours", json!(age)),
        ("review_rounds", json!(rounds)),
    ])
}

/// Zero while a critical item is under a live lease, else hours since the newest touch.
pub fn critical(r: &Readings) -> Fields {
    let path = r
        .critical_path
        .as_ref()
        .ok_or("critical path reading failed")?;
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let mut newest: Option<f64> = None;
    for item in items.iter().filter(|i| open(i) && path.contains(&i.key)) {
        if let Some(lease) = item.lease_until.as_deref() {
            if item.state == "claimed" && parse_time(lease)? > r.now {
                return fields(&[("critical_idle_hours", json!(0.0))]);
            }
        }
        let touched = updated(item)?;
        newest = Some(newest.map_or(touched, |n| n.max(touched)));
    }
    let idle = newest.map_or(0.0, |touched| (r.now - touched).max(0.0) / 3600.0);
    fields(&[("critical_idle_hours", json!(idle))])
}

pub fn placement(r: &Readings) -> Fields {
    let series = r.series.as_ref().ok_or("fleet series reading failed")?;
    if r.coding_hosts.is_empty() || r.worker_hosts.is_empty() {
        return Err("coding and worker hosts are not configured".into());
    }
    let on = |hosts: &[String]| {
        series
            .iter()
            .filter(|s| hosts.contains(&s.host))
            .collect::<Vec<_>>()
    };
    let runs = on(&r.coding_hosts).iter().map(|s| s.runs).sum::<u64>();
    let idle = on(&r.worker_hosts)
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
