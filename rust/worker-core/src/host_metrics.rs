//! Pure projection of injected host samples and authoritative run records.
use crate::hosts::{Caps, HostClass, HostRegistry};
use crate::run::record::{valid_run_id, Status};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostSample {
    pub host_id: String,
    pub received_at_ms: u64,
    pub cpu_percent: Option<f64>,
    pub ram_available_bytes: Option<u64>,
    pub gpu_percent: Option<f64>,
    pub network_rx_bytes_per_s: Option<u64>,
    pub network_tx_bytes_per_s: Option<u64>,
    pub disk_free_bytes: Option<u64>,
}

/// Invalid percentages become unknown independently; measured zero survives.
pub fn normalize_sample(mut sample: HostSample) -> HostSample {
    let valid = |n: &f64| n.is_finite() && (0.0..=100.0).contains(n);
    sample.cpu_percent = sample.cpu_percent.filter(valid);
    sample.gpu_percent = sample.gpu_percent.filter(valid);
    sample
}

#[derive(Debug, Clone, Serialize)]
pub struct FleetHostRow {
    pub host_id: String,
    pub class: HostClass,
    pub caps: Caps,
    pub sample: Option<HostSample>,
    pub running_agents: Option<u64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FleetSummary {
    pub hosts: Vec<FleetHostRow>,
    pub running_agents: Option<u64>,
}

/// One sorted row per registered host. No I/O, clock, or liveness inference.
/// Records need host, valid run_id and a recognized string status; attempts
/// in claimed/unknown/cancelling states are accepted but never counted.
pub fn summarize(
    registry: &HostRegistry,
    samples: &[HostSample],
    records: Option<&[Value]>,
) -> FleetSummary {
    let mut rows: BTreeMap<_, _> = registry
        .hosts
        .iter()
        .map(|h| {
            (
                h.id.clone(),
                FleetHostRow {
                    host_id: h.id.clone(),
                    class: h.class,
                    caps: h.caps.clone(),
                    sample: None,
                    running_agents: records.map(|_| 0),
                },
            )
        })
        .collect();
    for sample in samples {
        let Some(host) = registry.resolve(&sample.host_id) else {
            continue;
        };
        let row = rows.get_mut(&host.id).unwrap();
        if row
            .sample
            .as_ref()
            .is_none_or(|old| sample.received_at_ms >= old.received_at_ms)
        {
            let mut sample = normalize_sample(sample.clone());
            sample.host_id.clone_from(&host.id);
            row.sample = Some(sample);
        }
    }
    let mut bad_hosts = BTreeSet::new();
    let mut unassignable = false;
    let mut seen = BTreeMap::new();
    for record in records.into_iter().flatten() {
        let Some(host) = record
            .get("host")
            .and_then(Value::as_str)
            .and_then(|h| registry.resolve(h))
        else {
            unassignable = true;
            continue;
        };
        let id = record
            .get("run_id")
            .and_then(Value::as_str)
            .filter(|id| valid_run_id(id));
        let status = record.get("status").and_then(Value::as_str).filter(|s| {
            Status::parse(s).is_some() || matches!(*s, "claimed" | "unknown" | "cancelling")
        });
        let (Some(id), Some(status)) = (id, status) else {
            bad_hosts.insert(host.id.clone());
            continue;
        };
        if let Some(previous) = seen.insert((host.id.as_str(), id), status) {
            if previous != status {
                bad_hosts.insert(host.id.clone());
            }
        }
    }
    for ((host, _), status) in seen {
        if status == "running" {
            if let Some(count) = &mut rows.get_mut(host).unwrap().running_agents {
                *count += 1;
            }
        }
    }
    for row in rows.values_mut() {
        if unassignable || bad_hosts.contains(&row.host_id) {
            row.running_agents = None;
        }
    }
    let running_agents = if records.is_none() || unassignable || !bad_hosts.is_empty() {
        None
    } else {
        rows.values().map(|row| row.running_agents).sum()
    };
    FleetSummary {
        hosts: rows.into_values().collect(),
        running_agents,
    }
}

#[cfg(test)]
#[path = "host_metrics_tests.rs"]
mod tests;
