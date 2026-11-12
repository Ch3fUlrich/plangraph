//! Read-only fleet projection; fixed public errors only.
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
const METRICS: [&str; 6] = [
    "cpu_percent",
    "ram_available_bytes",
    "gpu_percent",
    "network_rx_bytes_per_s",
    "network_tx_bytes_per_s",
    "disk_free_bytes",
];
fn metric(v: &Value, key: &str) -> Value {
    match v.as_f64() {
        Some(n)
            if n.is_finite()
                && n >= 0.0
                && (!(key == "cpu_percent" || key == "gpu_percent") || n <= 100.0) =>
        {
            v.clone()
        }
        _ => Value::Null,
    }
}
pub(crate) fn build(data: &Value) -> Result<Value, String> {
    let mut hosts = BTreeMap::new();
    let mut aliases = HashMap::new();
    for (index, row) in data["registry"]
        .as_array()
        .ok_or("invalid registry")?
        .iter()
        .enumerate()
    {
        let host = ["id", "host_id", "host"]
            .iter()
            .map(|k| &row[k])
            .find(|v| {
                !v.is_null() && **v != json!("") && **v != json!(false) && v.as_f64() != Some(0.0)
            })
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or("registry host needs an id")?;
        if hosts.insert(host, (index, row)).is_some() {
            return Err("duplicate registry host".into());
        }
        let extra = match row.get("aliases") {
            None => &[][..],
            Some(v) => v
                .as_array()
                .ok_or("registry aliases must be a list or tuple of strings")?
                .as_slice(),
        };
        for alias in std::iter::once(host).chain(extra.iter().map(|v| v.as_str().unwrap_or(""))) {
            if alias.is_empty() {
                return Err("registry alias needs a nonempty string".into());
            }
            if aliases.insert(alias, host).is_some_and(|old| old != host) {
                return Err("ambiguous registry alias".into());
            }
        }
    }
    let mut known = !data["runs"].is_null();
    let mut seen = HashMap::new();
    for run in data["runs"].as_array().into_iter().flatten() {
        let host = run["host"].as_str().and_then(|h| aliases.get(h));
        let id = run["run_id"].as_str().filter(|s| !s.is_empty());
        let status = run["status"].as_str().filter(|s| {
            [
                "running",
                "completed",
                "failed",
                "cancelled",
                "queued",
                "dry_run",
                "refused",
            ]
            .contains(s)
        });
        if let (Some(host), Some(id), Some(status)) = (host, id, status) {
            if seen
                .insert((*host, id), status)
                .is_some_and(|old| old != status)
            {
                known = false;
            }
        } else {
            known = false;
        }
    }
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for ((host, _), status) in seen {
        if status == "running" {
            *counts.entry(host).or_default() += 1;
        }
    }
    let mut samples = HashMap::new();
    for pair in data["samples"].as_array().into_iter().flatten() {
        if let Some(host) = pair[0].as_str().and_then(|k| aliases.get(k)) {
            samples.insert(*host, &pair[1]);
        }
    }
    let rows: Vec<_> = hosts.into_iter().map(|(host, (index, row))| {
        let sample = samples.get(host).copied().unwrap_or(&Value::Null);
        let metrics: serde_json::Map<String, Value> = METRICS.iter()
            .map(|key| (key.to_string(), metric(&sample[key], key))).collect();
        json!({"host_id":host, "registry_index":index,
            "class":row["class"], "caps":row["caps"], "sample":metrics,
            "samples":if sample.is_object() {"available"} else {"unavailable"},
            "running_agents":if known {json!(counts.get(host).copied().unwrap_or(0))} else {Value::Null}})
    }).collect();
    Ok(
        json!({"hosts":rows, "running_agents":if known {json!(counts.values().sum::<usize>())} else {Value::Null}}),
    )
}
pub(crate) fn run() -> Result<Value, String> {
    let data: Value =
        serde_json::from_reader(std::io::stdin()).map_err(|_| "invalid fleet input")?;
    Ok(match build(&data) {
        Ok(value) => value,
        Err(reason) => json!({"error": reason}),
    })
}
#[cfg(test)]
#[path = "fleet_snapshot_tests.rs"]
mod tests;
