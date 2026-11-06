//! Inactive episode delivery adapter; no caller, timer or lifecycle actions.
//! One stable namespace per detector; persist this state with detector state after
//! every call, retaining the caller-owned monotonic epoch across JSON restoration.
//! Call `silence.tick` independently with acknowledged heartbeat time and its own
//! operator sink, even without findings or after primary exhaustion. Primary acks
//! are not heartbeat acks. No recursive monitor watches secondary delivery.
//! Later activation requires reviewed before/after flags, rollback by removing
//! that caller, separate exact-SHA live GO and immutable per-SHA run records.
use crate::fleet_alarm_delivery::{Deliveries, HeartbeatMonitor};
use crate::fleet_stale_pair_alert::{Config, Finding};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Delivery {
    namespace: String,
    latest_episode: u64,
    active_id: Option<String>,
    pub primary: Deliveries,
    pub silence: HeartbeatMonitor,
}

impl Delivery {
    pub fn new(namespace: String, started_at: u64, config: &Config) -> Result<Self, &'static str> {
        let attempts =
            u32::try_from(config.maximum_attempts).map_err(|_| "attempt limit overflow")?;
        if namespace.trim().is_empty() {
            return Err("namespace required");
        }
        Ok(Self {
            silence: HeartbeatMonitor::new(
                format!("{namespace}:heartbeat"),
                started_at,
                config.silence_deadline,
                config.retry_delay,
                attempts,
            )?,
            primary: Deliveries::new(config.retry_delay, attempts)?,
            namespace,
            latest_episode: 0,
            active_id: None,
        })
    }

    pub fn tick<E>(
        &mut self,
        finding: Option<&Finding>,
        acknowledgement: Option<&str>,
        now: u64,
        operator_sink: &mut impl FnMut(&str) -> Result<(), E>,
    ) -> bool {
        if let Some(episode) = finding.and_then(|f| f.episode) {
            if episode > self.latest_episode {
                self.latest_episode = episode;
                self.active_id = Some(format!("{}:pair:{episode}", self.namespace));
            }
        }
        let Some(id) = &self.active_id else {
            return false;
        };
        if acknowledgement == Some(id.as_str()) {
            self.primary.acknowledge(id);
        }
        self.primary.tick(id, now, operator_sink)
    }
}

#[cfg(test)]
#[path = "fleet_stale_pair_delivery_tests.rs"]
mod tests;
