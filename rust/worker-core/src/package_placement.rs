//! Shared package/spawner host admission. No model calls or process launching.
use crate::{
    fleet_series::{self, Sample},
    host_readiness::{Record, WRITER_SANDBOX},
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
    pub readiness: Option<Record>,
}
#[derive(Clone, Debug)]
pub struct Policy {
    pub allow_workstation: bool,
    pub floor_mb: u64,
    pub reservation_mb: u64,
    pub max_load_per_core: f64,
    pub host_limits: BTreeMap<String, (u64, u64)>,
    pub excluded_hosts: BTreeSet<String>,
    pub fallback_hosts: BTreeSet<String>,
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
            fallback_hosts: BTreeSet::new(),
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
            if host.placement_fallback {
                self.fallback_hosts.insert(host.id.clone());
            }
            self.host_limits.insert(
                host.id.clone(),
                (
                    host.caps.mem_floor_mb,
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
    } else if let Some(reason) = candidate
        .readiness
        .as_ref()
        .map_or(Some("missing readiness"), |record| {
            record.refusal(&candidate.id, WRITER_SANDBOX, now)
        })
    {
        reason
    } else if let Some(row) = &candidate.sample {
        if row.host != candidate.id {
            "sample identity mismatch"
        } else if !row.reachable {
            "unreachable"
        } else if row.ts > now {
            "future sample"
        } else if now - row.ts > 120 {
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
/// Rank admitted hosts by the registry floor, placing explicit fallback hosts last.
pub fn ranked<'a>(
    hosts: &'a [Candidate],
    policy: &Policy,
    now: u64,
) -> (Vec<&'a Candidate>, Vec<Refusal>) {
    let mut refusals = Vec::new();
    let mut admitted = Vec::new();
    for host in hosts {
        if let Some(reason) = refusal(host, policy, now) {
            refusals.push(Refusal {
                host: host.id.clone(),
                reason,
            });
        } else {
            admitted.push(host);
        }
    }
    admitted.sort_by_key(|host| {
        (
            policy.fallback_hosts.contains(&host.id),
            std::cmp::Reverse(headroom(host, policy)),
        )
    });
    (admitted, refusals)
}

fn headroom(host: &Candidate, policy: &Policy) -> u64 {
    let floor = policy
        .host_limits
        .get(&host.id)
        .map_or(policy.floor_mb, |limits| limits.0);
    host.sample
        .as_ref()
        .and_then(|sample| sample.mem_avail_mb)
        .unwrap_or_default()
        .saturating_sub(floor)
}

pub fn placement_reason(host: &Candidate, policy: &Policy) -> String {
    let row = host
        .sample
        .as_ref()
        .expect("admitted host has a capacity sample");
    let fallback = if policy.fallback_hosts.contains(&host.id) {
        "fallback, "
    } else {
        ""
    };
    format!(
        "admitted: {fallback}{} MB above floor, load/core={:.2}",
        headroom(host, policy),
        row.cpu_load.unwrap() / row.cores as f64,
    )
}

pub fn choose(hosts: &[Candidate], policy: &Policy, now: u64) -> Choice {
    let (admitted, refusals) = ranked(hosts, policy, now);
    match admitted.first() {
        Some(host) => Choice {
            host: Some(host.id.clone()),
            reason: placement_reason(host, policy),
            refusals,
        },
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
            ssh: h.ssh_route(),
            readiness: Record::read(state, &h.id, WRITER_SANDBOX).ok(),
            sample: fleet_series::read(&state.join("fleet").join(format!("{}.ndjson", h.id)))
                .ok()
                .and_then(|rows| {
                    rows.into_iter()
                        .filter(|r| r.host == h.id)
                        .max_by_key(|s| s.ts)
                }),
        })
        .collect();
    if let Some(local) = hosts.iter_mut().find(|h| h.id == registry.self_host().id) {
        refresh_with(std::slice::from_mut(local), now, |h| {
            let probe = crate::host_probe::probe(&h.id).map_err(io::Error::other)?;
            Ok(Sample {
                host: h.id.clone(),
                ts: now,
                cpu_load: probe.load1,
                mem_avail_mb: Some(probe.mem_available_mb),
                swap_used_mb: Some(probe.swap_total_mb.saturating_sub(probe.swap_free_mb)),
                cores: probe.cpus,
                reachable: true,
                ..Sample::default()
            })
        });
    }
    hosts
}
