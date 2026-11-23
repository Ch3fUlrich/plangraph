//! Compare the observed terminal-run inventory with Keeper receipts for one UTC day.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

#[derive(Deserialize, Serialize)]
struct Identity {
    run_id: String,
    host: String,
}

#[derive(Deserialize)]
struct Evidence {
    day: String,
    runs: Vec<Identity>,
    fleet: Vec<Identity>,
    keeper: Vec<Identity>,
}

#[derive(Default, Serialize)]
struct Counts {
    expected: u64,
    keeper: u64,
    matched: u64,
}

fn day_start(day: &str) -> Result<i64, String> {
    chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
        .ok()
        .filter(|date| date.format("%Y-%m-%d").to_string() == day)
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|date| date.and_utc().timestamp())
        .ok_or_else(|| "expected a UTC day as YYYY-MM-DD".into())
}

fn inventory(rows: Vec<Identity>) -> Result<BTreeMap<String, String>, String> {
    let mut inventory = BTreeMap::new();
    for row in rows {
        if row.run_id.is_empty() || row.host.is_empty() {
            return Err("attempt coverage requires a run ID and a host".into());
        }
        if let Some(previous) = inventory.insert(row.run_id, row.host.clone()) {
            if previous != row.host {
                return Err("conflicting hosts for one attempt coverage run ID".into());
            }
        }
    }
    Ok(inventory)
}

fn report(evidence: Evidence) -> Result<Value, String> {
    day_start(&evidence.day)?;
    let mut expected = inventory(evidence.runs)?;
    for (id, host) in inventory(evidence.fleet)? {
        expected.entry(id).or_insert(host);
    }
    let keeper = inventory(evidence.keeper)?;
    let mut hosts = BTreeMap::<String, Counts>::new();
    let mut total = Counts::default();
    for host in keeper.values() {
        hosts.entry(host.clone()).or_default().keeper += 1;
        total.keeper += 1;
    }
    for (id, host) in expected {
        let matched = u64::from(keeper.get(&id) == Some(&host));
        let counts = hosts.entry(host).or_default();
        counts.expected += 1;
        counts.matched += matched;
        total.expected += 1;
        total.matched += matched;
    }
    let below = hosts
        .values()
        .any(|row| row.matched * 100 < row.expected * 95);
    Ok(json!({"day":evidence.day, "total":total, "hosts":hosts, "below_threshold":below}))
}

fn inventory_paths(
    directory: &Path,
    path: impl Fn(PathBuf) -> PathBuf,
    start: i64,
    host: &str,
) -> Result<Vec<Identity>, String> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error.to_string()),
    };
    let mut result = Vec::new();
    for entry in entries {
        let path = path(entry.map_err(|error| error.to_string())?.path());
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.to_string()),
        };
        let record: Value = serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if !crate::attempt_outcome::terminal(&record)
            || !record["ended"]
                .as_f64()
                .is_some_and(|end| start as f64 <= end && end < (start + 86_400) as f64)
        {
            continue;
        }
        if let Some(id) = record["run_id"].as_str().filter(|id| !id.is_empty()) {
            result.push(Identity {
                run_id: id.to_owned(),
                host: record["host"]
                    .as_str()
                    .filter(|host| !host.is_empty())
                    .unwrap_or(host)
                    .to_owned(),
            });
        }
    }
    Ok(result)
}

fn read_local(day: &str) -> Result<Value, String> {
    let start = day_start(day)?;
    let state = crate::state::resolve_state(None, |key| std::env::var_os(key))?;
    let host = std::env::var("PLANGRAPH_HOST").unwrap_or_default();
    #[cfg(target_os = "linux")]
    let host = if host.is_empty() {
        pg_spawnd::session::local_hostname()
    } else {
        host
    };
    #[cfg(windows)]
    let host = if host.is_empty() {
        std::env::var("COMPUTERNAME").unwrap_or_default()
    } else {
        host
    };
    let runs = inventory_paths(
        &state.join("runs"),
        |path| path.join("record.json"),
        start,
        &host,
    )?;
    #[cfg(target_os = "linux")]
    let fleet = inventory_paths(
        &state.join(pg_spawnd::external_run::FLEET_OUTCOMES),
        |path| path,
        start,
        &host,
    )?;
    #[cfg(not(target_os = "linux"))]
    let fleet: Vec<Identity> = Vec::new();
    Ok(json!({"day":day, "runs":runs, "fleet":fleet}))
}

pub(crate) fn execute(verb: &str, args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let result = match (verb, args) {
        ("attempt-coverage-input", [day]) => read_local(day)?,
        ("attempt-coverage", []) => {
            let evidence =
                serde_json::from_reader(io::stdin().lock()).map_err(|err| err.to_string())?;
            report(evidence)?
        }
        _ => {
            return Err(
                "usage: attempt-coverage-input YYYY-MM-DD | attempt-coverage (JSON stdin)".into(),
            )
        }
    };
    writeln!(out, "{result}").map_err(|err| err.to_string())
}
