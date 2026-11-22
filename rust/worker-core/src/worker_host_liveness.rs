//! Preserve outage evidence while the worker and its run records are unavailable.
use super::{Phase, State, Store};
use crate::{host_link, run::record::valid_run_id};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeSet, io};

pub struct Observation {
    pub now: u64,
    pub ssh: bool,
    pub tailscale: Option<bool>,
    pub remote_runs: Option<BTreeSet<String>>,
    pub placed_runs: BTreeSet<String>,
}

#[derive(Default, Deserialize, Serialize)]
pub(super) struct Link {
    pub(super) latest_tailscale: Option<bool>,
    last_inventory_at: Option<u64>,
    last_runs: BTreeSet<String>,
    pub(super) unknown_runs: BTreeSet<String>,
    announced: Option<Announcement>,
    pub(super) maintenance_end: Option<u64>,
    sequence: u64,
    pending: Vec<Value>,
}

#[derive(Clone, Copy, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Announcement {
    Unreachable,
    Maintenance,
    Recovered,
}

impl Announcement {
    fn rule(self) -> &'static str {
        match self {
            Self::Unreachable => "worker-host-unreachable",
            Self::Maintenance => "worker-host-maintenance",
            Self::Recovered => "worker-host-recovered",
        }
    }
}

impl Store {
    /// Refuse placement immediately through the declared end and the subsequent rejoin.
    pub fn set_maintenance(&self, now: u64, until: u64) -> io::Result<()> {
        let _guard = self.lock("lock")?;
        let mut state = self.load()?;
        if until <= now || i64::try_from(until).is_err() || now < state.observed_at {
            return Err(io::Error::other("maintenance end must be in the future"));
        }
        state.phase = Phase::Rejoining;
        state.maintenance_until = Some(until);
        state.link.maintenance_end = Some(until);
        state.episode = state.episode.saturating_add(1);
        self.save(&state)
    }

    pub fn unknown_runs(&self) -> io::Result<BTreeSet<String>> {
        Ok(self.load()?.link.unknown_runs)
    }

    /// Record one completed probe, persist pending alerts, and queue them for Keeper delivery.
    pub fn observe_liveness(&self, observed: Observation) -> io::Result<(Phase, bool)> {
        if observed
            .placed_runs
            .iter()
            .chain(observed.remote_runs.iter().flatten())
            .any(|id| !valid_run_id(id))
        {
            return Err(io::Error::other("invalid worker run inventory"));
        }
        let _guard = self.lock("lock")?;
        let mut state = self.load()?;
        let was_down = state.outage_since;
        state.advance(observed.now, observed.ssh, None)?;
        let in_maintenance = state.maintenance_until.is_some();
        let link = &mut state.link;
        link.latest_tailscale = observed.tailscale;
        if let Some(runs) = observed.remote_runs {
            link.last_runs = runs;
            link.last_inventory_at = Some(observed.now);
        }

        if !observed.ssh {
            link.unknown_runs.extend(observed.placed_runs);
            link.unknown_runs.extend(link.last_runs.iter().cloned());
        }

        let notice = if observed.ssh {
            link.announced.take().map(|_| Announcement::Recovered)
        } else if state.missed >= 2 {
            let next = if in_maintenance {
                Announcement::Maintenance
            } else {
                Announcement::Unreachable
            };
            let changed = link.announced.replace(next) != Some(next);
            changed.then_some(next)
        } else {
            None
        };

        if let Some(notice) = notice {
            let since = was_down.or(state.outage_since).expect("announced outage");
            let maintenance_end = link.maintenance_end;
            let recipient = if in_maintenance {
                "host-health"
            } else {
                "routing"
            };
            link.sequence = link
                .sequence
                .checked_add(1)
                .ok_or_else(|| io::Error::other("worker alert sequence exhausted"))?;
            link.pending.push(json!({
                "kind":"alert", "rule":notice.rule(), "host":self.host, "recipient":recipient,
                "item_key":"plangraph:LE-WORKER-HOSTS-UNREACHABLE-UNALERTED",
                "observed_at":observed.now, "down_since":since, "sequence":link.sequence,
                "unknown_run_ids":link.unknown_runs, "maintenance_until":maintenance_end,
                "tailscale_reachable":observed.tailscale, "inventory_at":link.last_inventory_at,
            }));
        }
        if observed.ssh && state.phase == Phase::Ready {
            link.unknown_runs.clear();
            link.maintenance_end = None;
        }
        let phase = state.phase;
        // The placement hold survives an outbox write failure.
        self.save(&state)?;
        self.drain_alerts(&mut state)?;
        Ok((
            phase,
            phase == Phase::Rejoining && observed.ssh && !in_maintenance,
        ))
    }

    fn drain_alerts(&self, state: &mut State) -> io::Result<()> {
        if state.link.pending.is_empty() {
            return Ok(());
        }
        for event in &state.link.pending {
            host_link::enqueue_worker_alert(&self.root, event)?;
        }
        state.link.pending.clear();
        self.save(state)
    }
}
