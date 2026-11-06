//! Inactive, injected alarm delivery; no signal evaluation or lifecycle actions.
//! All times are seconds in one caller-owned monotonic epoch, including restore.
//! Persist state after each call. A caller must never reuse an episode ID for a
//! different episode. Transport success is deliberately not an acknowledgement.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Delivery {
    pub attempts: u32,
    pub acknowledged: bool,
    pub next_retry: Option<u64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Deliveries {
    retry_delay: u64,
    maximum_attempts: u32,
    records: BTreeMap<String, Delivery>,
}

impl Deliveries {
    pub fn new(retry_delay: u64, maximum_attempts: u32) -> Result<Self, &'static str> {
        if retry_delay == 0 || maximum_attempts == 0 {
            return Err("retry delay and attempt limit must be positive");
        }
        Ok(Self {
            retry_delay,
            maximum_attempts,
            records: BTreeMap::new(),
        })
    }

    pub fn record(&self, id: &str) -> Option<&Delivery> {
        self.records.get(id)
    }

    /// True only for a known, previously attempted ID. Late acknowledgements of
    /// another episode cannot complete this episode, even after exhaustion.
    pub fn acknowledge(&mut self, id: &str) -> bool {
        let Some(record) = self.records.get_mut(id) else {
            return false;
        };
        if record.attempts == 0 || record.acknowledged {
            return false;
        }
        record.acknowledged = true;
        record.next_retry = None;
        true
    }

    /// Returns whether a send was attempted; sink errors/drops consume attempts.
    /// Retained records deduplicate completed and exhausted episodes on restore.
    pub fn tick<E>(
        &mut self,
        id: &str,
        now: u64,
        sink: &mut impl FnMut(&str) -> Result<(), E>,
    ) -> bool {
        if id.is_empty() || self.retry_delay == 0 || self.maximum_attempts == 0 {
            return false;
        }
        let record = self.records.entry(id.into()).or_insert(Delivery {
            attempts: 0,
            acknowledged: false,
            next_retry: Some(now),
        });
        if record.acknowledged
            || record.attempts >= self.maximum_attempts
            || !record.next_retry.is_some_and(|due| now >= due)
        {
            return false;
        }
        record.attempts += 1;
        record.next_retry = if record.attempts < self.maximum_attempts {
            now.checked_add(self.retry_delay)
        } else {
            None
        };
        let _ = sink(id);
        true
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Findings {
    pub silence_id: Option<String>,
    pub clock_discontinuity: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HeartbeatMonitor {
    namespace: String,
    silence_after: u64,
    last_now: u64,
    last_heartbeat: u64,
    episode: u64,
    active: Option<String>,
    pub delivery: Deliveries,
}

impl HeartbeatMonitor {
    /// `started_at` is the silence baseline when no heartbeat has been received.
    /// Namespace must be stable and distinct from primary alarm IDs.
    pub fn new(
        namespace: String,
        started_at: u64,
        silence_after: u64,
        retry_delay: u64,
        maximum_attempts: u32,
    ) -> Result<Self, &'static str> {
        if namespace.is_empty() || silence_after == 0 {
            return Err("namespace and positive silence interval required");
        }
        Ok(Self {
            namespace,
            silence_after,
            last_now: started_at,
            last_heartbeat: started_at,
            episode: 0,
            active: None,
            delivery: Deliveries::new(retry_delay, maximum_attempts)?,
        })
    }

    /// Independently callable: no samples, primary state, or recursive monitor.
    /// Only a newer, valid acknowledged heartbeat rearms silence. Clock faults
    /// retain watermarks and cannot advance counters or masquerade as recovery.
    pub fn tick<E>(
        &mut self,
        last_acknowledged_heartbeat: Option<u64>,
        now: u64,
        operator_sink: &mut impl FnMut(&str) -> Result<(), E>,
    ) -> Findings {
        let mut out = Findings::default();
        let backwards = now < self.last_now;
        out.clock_discontinuity = backwards
            || last_acknowledged_heartbeat.is_some_and(|t| t > now || t < self.last_heartbeat);
        if backwards {
            return out;
        }
        self.last_now = now;
        if !out.clock_discontinuity {
            if let Some(t) = last_acknowledged_heartbeat.filter(|t| *t > self.last_heartbeat) {
                self.last_heartbeat = t;
                if now.saturating_sub(t) < self.silence_after {
                    self.active = None;
                }
            }
        }
        if now.saturating_sub(self.last_heartbeat) >= self.silence_after {
            if self.active.is_none() {
                let Some(episode) = self.episode.checked_add(1) else {
                    out.clock_discontinuity = true;
                    return out;
                };
                self.episode = episode;
                let id = format!("{}:silence:{episode}", self.namespace);
                out.silence_id = Some(id.clone());
                self.active = Some(id);
            }
            if let Some(id) = &self.active {
                self.delivery.tick(id, now, operator_sink);
            }
        }
        out
    }
}

#[cfg(test)]
#[path = "fleet_alarm_delivery_tests.rs"]
mod tests;
