//! Shared package/spawner host admission. No model calls or process launching.
use crate::{
    fleet_collect,
    fleet_series::{self, Sample},
    hosts::{HostRegistry, Os},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    path::Path,
};

#[derive(Clone, Debug, Deserialize)]
pub struct Candidate {
    pub id: String,
    #[serde(default)]
    pub workstation: bool,
    pub ssh: Option<String>,
    pub sample: Option<Sample>,
}
#[derive(Clone, Debug)]
pub struct Policy {
    pub allow_workstation: bool,
    pub floor_mb: u64,
    pub reservation_mb: u64,
    pub max_load_per_core: f64,
    pub host_limits: BTreeMap<String, (u64, u64)>,
    pub excluded_hosts: BTreeSet<String>,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            allow_workstation: false,
            floor_mb: 3000,
            reservation_mb: 2048,
            max_load_per_core: 1.0,
            host_limits: BTreeMap::new(),
            excluded_hosts: BTreeSet::new(),
        }
    }
}
impl Policy {
    pub fn apply_registry(&mut self, registry: &HostRegistry, role: crate::hosts::Role) {
        let key = serde_json::to_value(role)
            .unwrap()
            .as_str()
            .unwrap()
            .to_owned();
        for host in &registry.hosts {
            self.host_limits.insert(
                host.id.clone(),
                (
                    self.floor_mb.max(host.caps.mem_floor_mb),
                    self.reservation_mb
                        .max(host.warmup.ramp_mb.get(&key).copied().unwrap_or(0)),
                ),
            );
        }
    }
}
#[derive(Debug, Serialize)]
pub struct Refusal {
    pub host: String,
    pub reason: String,
}
#[derive(Debug, Serialize)]
pub struct Choice {
    pub host: Option<String>,
    pub reason: String,
    pub refusals: Vec<Refusal>,
}

pub fn refusal(candidate: &Candidate, policy: &Policy, now: u64) -> Option<String> {
    let (floor, reservation) = policy
        .host_limits
        .get(&candidate.id)
        .copied()
        .unwrap_or((policy.floor_mb, policy.reservation_mb));
    let reason = if policy.excluded_hosts.contains(&candidate.id) {
        "host excluded by readiness preflight"
    } else if candidate.workstation && !policy.allow_workstation {
        "workstation not allowed"
    } else if !fleet_series::valid_host(&candidate.id) {
        "invalid host id"
    } else if let Some(row) = &candidate.sample {
        if row.host != candidate.id {
            "sample identity mismatch"
        } else if !row.reachable {
            "unreachable"
        } else if row.ts > now || now - row.ts > 120 {
            "stale sample"
        } else if row
            .mem_avail_mb
            .is_none_or(|m| m.checked_sub(reservation).is_none_or(|left| left < floor))
        {
            "memory below floor after reservation"
        } else if row.cores == 0 || row.cpu_load.is_none_or(|l| !l.is_finite() || l < 0.0) {
            "unknown capacity"
        } else if row.cpu_load.unwrap() / row.cores as f64 >= policy.max_load_per_core {
            "load gate"
        } else {
            return None;
        }
    } else {
        "missing sample"
    };
    Some(reason.into())
}
pub fn choose(hosts: &[Candidate], policy: &Policy, now: u64) -> Choice {
    let mut refusals = Vec::new();
    let mut admitting = Vec::new();
    for (index, host) in hosts.iter().enumerate() {
        if let Some(reason) = refusal(host, policy, now) {
            refusals.push(Refusal {
                host: host.id.clone(),
                reason,
            });
        } else {
            admitting.push((index, host));
        }
    }
    admitting.sort_by(|(ai, a), (bi, b)| {
        let (a, b) = (a.sample.as_ref().unwrap(), b.sample.as_ref().unwrap());
        (a.cpu_load.unwrap() / a.cores as f64)
            .total_cmp(&(b.cpu_load.unwrap() / b.cores as f64))
            .then_with(|| {
                a.swap_used_mb
                    .unwrap_or(u64::MAX)
                    .cmp(&b.swap_used_mb.unwrap_or(u64::MAX))
            })
            .then_with(|| a.runs.cmp(&b.runs))
            .then_with(|| a.sessions.len().cmp(&b.sessions.len()))
            .then_with(|| b.mem_avail_mb.cmp(&a.mem_avail_mb))
            .then_with(|| ai.cmp(bi))
    });
    match admitting.first() {
        Some((_, host)) => {
            let row = host.sample.as_ref().unwrap();
            Choice {
                host: Some(host.id.clone()),
                reason: format!(
                    "admitted: load/core={:.2}, available={} MB, swap={} MB, runs={}, sessions={}",
                    row.cpu_load.unwrap() / row.cores as f64,
                    row.mem_avail_mb.unwrap(),
                    row.swap_used_mb
                        .map_or_else(|| "unknown".into(), |m| m.to_string()),
                    row.runs,
                    row.sessions.len()
                ),
                refusals,
            }
        }
        None => Choice {
            host: None,
            reason: "queue: no host admits".into(),
            refusals,
        },
    }
}
pub fn refresh_with(
    hosts: &mut [Candidate],
    now: u64,
    mut probe: impl FnMut(&Candidate) -> io::Result<Sample>,
) {
    for host in hosts {
        if host
            .sample
            .as_ref()
            .is_none_or(|s| s.host != host.id || s.ts > now || now - s.ts > 120)
        {
            host.sample = Some(probe(host).unwrap_or_else(|_| Sample {
                host: host.id.clone(),
                ts: now,
                ..Sample::default()
            }));
        }
    }
}
pub fn load(registry: &HostRegistry, state: &Path, now: u64) -> Vec<Candidate> {
    load_for_role(registry, state, now, crate::hosts::Role::Implement)
}
pub fn load_for_role(
    registry: &HostRegistry,
    state: &Path,
    now: u64,
    role: crate::hosts::Role,
) -> Vec<Candidate> {
    let mut hosts: Vec<_> = registry
        .hosts
        .iter()
        .filter(|h| h.roles.contains(&role))
        .map(|h| Candidate {
            id: h.id.clone(),
            workstation: h.os == Os::Windows,
            ssh: h.ssh_target.clone(),
            sample: fleet_series::read(&state.join("fleet").join(format!("{}.ndjson", h.id)))
                .ok()
                .and_then(|rows| {
                    rows.into_iter()
                        .filter(|r| r.host == h.id)
                        .max_by_key(|s| s.ts)
                }),
        })
        .collect();
    refresh_with(&mut hosts, now, |h| {
        if h.id == registry.self_host {
            fleet_collect::collect(&h.id, state)
        } else {
            fleet_collect::remote(
                &h.id,
                h.ssh
                    .as_deref()
                    .ok_or_else(|| io::Error::other("no ssh route"))?,
            )
        }
    });
    hosts
}
