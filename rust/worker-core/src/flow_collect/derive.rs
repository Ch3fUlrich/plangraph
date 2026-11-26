//! Required watchdog measurements, derived without consulting live services.
use super::{detail, index, unique_hosts, CiRun, Detail, Evidence, Host, Keeper, Unit, Usage};
use super::{CI_WINDOW_S, DAY_S, SAMPLE_MAX_AGE_S};
use crate::fleet_series::Sample;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn parse_time(text: &str) -> Evidence<f64> {
    chrono::DateTime::parse_from_rfc3339(text)
        .map(|time| time.timestamp_millis() as f64 / 1000.0)
        .map_err(|_| "invalid observation timestamp".into())
}

pub(super) fn age(now: u64, stamp: &str) -> Evidence<f64> {
    let observed = parse_time(stamp)?;
    if !(0.0..=now as f64).contains(&observed) {
        return Err("observation timestamp is outside the collection interval".into());
    }
    Ok(now as f64 - observed)
}

pub(super) fn current(now: u64, observed: u64) -> bool {
    (now.saturating_sub(SAMPLE_MAX_AGE_S)..=now).contains(&observed)
}

pub(super) fn usage(usage: Usage) -> Evidence<Value> {
    if usage.budget_per_day <= 0.0 || usage.weighted < 0.0 {
        return Err("usage pace must be nonnegative and budget positive".into());
    }
    Ok(json!({"pace":usage.weighted,"budget":usage.budget_per_day}))
}

pub(super) fn ci(now: u64, runs: Vec<CiRun>) -> Evidence<Value> {
    let (mut total, mut failed, mut infra) = (0u64, 0u64, 0u64);
    for run in runs {
        let elapsed = now as f64 - parse_time(&run.created)?;
        if !(0.0..=CI_WINDOW_S as f64).contains(&elapsed) {
            continue;
        }
        match run.conclusion.as_str() {
            "success" => total += 1,
            "failure" => {
                total += 1;
                failed += 1;
            }
            "timed_out" | "startup_failure" => {
                total += 1;
                failed += 1;
                infra += 1;
            }
            "cancelled" | "skipped" | "neutral" | "stale" => {}
            _ => return Err("unrecognized completed CI conclusion".into()),
        }
    }
    Ok(json!({"ci_total":total,"ci_failed":failed,"infra_failed":infra}))
}

pub(super) fn review(
    now: u64,
    keeper: &Keeper,
    details: &Evidence<BTreeMap<String, Detail>>,
) -> Evidence<Value> {
    let mut longest = 0.0f64;
    let mut rounds = 0u64;
    for item in index(keeper)?
        .into_values()
        .filter(|item| item.state == "in_review")
    {
        let current = detail(details, item)?;
        let round = current
            .round
            .filter(|round| *round > 0)
            .ok_or_else(|| format!("no current Keeper review round for {}", item.key))?;
        let updated = item
            .updated_at
            .as_deref()
            .ok_or_else(|| format!("no Keeper review timestamp for {}", item.key))?;
        longest = longest.max(age(now, updated)?);
        rounds = rounds.max(round);
    }
    Ok(json!({"review_age_hours":longest / 3600.0,"review_rounds":rounds}))
}

/// The critical-path stall reader uses activity of claimed or reviewed open dependencies.
pub(super) fn critical(
    now: u64,
    keeper: &Keeper,
    target: &str,
    details: &Evidence<BTreeMap<String, Detail>>,
) -> Evidence<Value> {
    let items = index(keeper)?;
    let root = items
        .get(target)
        .ok_or_else(|| format!("critical target missing from Keeper snapshot: {target}"))?;
    let mut seen = BTreeSet::from([target]);
    let mut pending = root
        .depends_on
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>();
    let mut longest = 0.0f64;
    while let Some(key) = pending.pop() {
        if !seen.insert(key) {
            continue;
        }
        let item = items
            .get(key)
            .ok_or_else(|| format!("critical dependency missing from Keeper snapshot: {key}"))?;
        if matches!(item.state.as_str(), "done" | "cancelled") {
            continue;
        }
        pending.extend(item.depends_on.iter().map(String::as_str));
        if matches!(item.state.as_str(), "claimed" | "in_review") {
            let activity = detail(details, item)?
                .last_activity
                .as_ref()
                .ok_or_else(|| format!("no Keeper last activity for {key}"))?;
            longest = longest.max(age(now, &activity.at)?);
        }
    }
    Ok(json!({"critical_idle_hours":longest / 3600.0}))
}

pub(super) fn placement(
    now: u64,
    series: &[Sample],
    coding: Vec<String>,
    workers: Vec<String>,
) -> Evidence<Value> {
    let coding = unique_hosts(coding)?;
    let workers = unique_hosts(workers)?;
    let mut latest = BTreeMap::<&str, &Sample>::new();
    for sample in series {
        if !coding.contains(&sample.host) && !workers.contains(&sample.host) {
            continue;
        }
        let previous = latest.entry(&sample.host).or_insert(sample);
        if sample.ts >= previous.ts {
            *previous = sample;
        }
    }
    let sample = |host: &str| {
        let row = latest
            .get(host)
            .copied()
            .ok_or_else(|| format!("no fleet-series sample for {host}"))?;
        if !current(now, row.ts) {
            return Err(format!(
                "fleet-series sample for {host} is outside the current window"
            ));
        }
        if !row.reachable {
            return Err(format!("fleet-series sample for {host} is unreachable"));
        }
        Ok(row)
    };
    let mut runs = 0u64;
    for host in coding {
        runs = runs
            .checked_add(sample(&host)?.runs)
            .ok_or("coding run count overflow")?;
    }
    let mut idle = false;
    for host in workers {
        let row = sample(&host)?;
        idle |= row.runs == 0 && row.ready > 0;
    }
    Ok(json!({"coding_vm_runs":runs,"worker_idle":idle}))
}

pub(super) fn drift(hosts: &[Host], units: Vec<Unit>) -> Evidence<Value> {
    if hosts.is_empty()
        || hosts
            .iter()
            .any(|host| host.checkout_head.is_empty() || host.origin_main_head.is_empty())
    {
        return Err("host checkout or protected-main ancestry unavailable".into());
    }
    if units
        .iter()
        .any(|unit| unit.expected_sha.is_empty() || unit.deployed_sha.as_deref() == Some(""))
    {
        return Err("unit inventory contains an unusable SHA".into());
    }
    let code_drift = hosts.iter().any(|row| row.behind_commits > 0);
    let unit_drift = units
        .iter()
        .any(|unit| unit.deployed_sha.as_deref() != Some(unit.expected_sha.as_str()));
    Ok(json!({"host_drift":code_drift || unit_drift}))
}

pub(super) fn hosts(rows: &Vec<Host>) -> Evidence<Value> {
    if let Some(row) = rows.iter().find(|row| row.measurements.is_none()) {
        return Err(format!(
            "disk observation for {}: {}",
            row.host,
            row.disk_error
                .as_deref()
                .unwrap_or("missing or malformed reading")
        ));
    }
    Ok(json!({"hosts":rows}))
}

pub(super) fn closes(now: u64, keeper: &Keeper) -> Evidence<Value> {
    let (mut recent, mut previous) = (0u64, 0u64);
    for event in keeper.activity.iter().filter(|event| {
        matches!(
            event.kind.as_str(),
            "review_passed" | "closed_on_decision" | "answered"
        )
    }) {
        let elapsed = age(now, &event.ts)?;
        if elapsed < DAY_S as f64 {
            recent += 1;
        } else if elapsed < (2 * DAY_S) as f64 {
            previous += 1;
        }
    }
    Ok(json!({"closes_recent":recent,"closes_previous":previous}))
}
