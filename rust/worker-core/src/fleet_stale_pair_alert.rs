//! Dark, seconds-based JSON observation only; no delivery or lifecycle integration.
//! One State belongs to one ordered pair. Delivery, retries and silence monitoring
//! are part 2; their configuration is retained here, not executed.
//! Inactive: no caller/activation. Later ENABLE-FLEET-FOUNDATION requires separate
//! exact-SHA live GO, reviewed before/after flags and immutable per-SHA run records.
//! Rollback removes that later caller/activation, never changes lifecycle policy.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Config {
    pub maximum_age: u64,
    pub divergence_tolerance: u64,
    pub compaction_cap: u64,
    pub silence_deadline: u64,
    pub retry_delay: u64,
    pub maximum_attempts: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Member {
    pub identity: String,
    pub revision: String,
    pub checkpoint: String,
    pub freshness: Option<i64>,
    pub provenance: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub members: [Member; 2],
    pub compactions: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Clock {
    pub wall: i64,
    pub monotonic: u64,
    /// None certifies no discontinuity; otherwise an injected diagnostic.
    pub discontinuity: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Finding {
    pub evidence: Option<Snapshot>,
    pub ages: [Option<u64>; 2],
    pub divergence: Option<u64>,
    pub elapsed_compactions: Option<u64>,
    pub diagnostics: Vec<String>,
    pub reasons: Vec<String>,
    pub episode: Option<u64>,
    pub new_alarm: bool,
}
#[derive(Default, Clone, Debug, Serialize, Deserialize)]
pub struct State {
    coherent: Option<Snapshot>,
    evidence: [Option<Member>; 2],
    ages: [Option<u64>; 2],
    monotonic: Option<u64>,
    count: Option<u64>,
    baseline: Option<u64>,
    episode: u64,
    active: bool,
}
impl State {
    pub fn observe(&mut self, config: &Config, bytes: &[u8], clock: Clock) -> Finding {
        let mut out = Finding {
            evidence: serde_json::from_slice(bytes).ok(),
            ages: [None; 2],
            divergence: None,
            elapsed_compactions: None,
            diagnostics: vec![],
            reasons: vec![],
            episode: self.active.then_some(self.episode),
            new_alarm: false,
        };
        if [
            config.maximum_age,
            config.divergence_tolerance,
            config.compaction_cap,
            config.silence_deadline,
            config.retry_delay,
            config.maximum_attempts,
        ]
        .contains(&0)
        {
            out.diagnostics.push("invalid configuration".into());
            return out;
        }
        let delta = self
            .monotonic
            .map_or(Some(0), |t| clock.monotonic.checked_sub(t));
        if let Some(dt) = delta {
            self.monotonic = Some(clock.monotonic);
            for age in &mut self.ages {
                *age = age.map(|a| {
                    a.checked_add(dt).unwrap_or_else(|| {
                        out.diagnostics.push("age overflow: saturated".into());
                        u64::MAX
                    })
                });
            }
        } else {
            out.diagnostics.push("monotonic regression".into());
        }
        if let Some(reason) = &clock.discontinuity {
            out.diagnostics
                .push(format!("wall discontinuity: {reason}"));
        }
        if let Some(s) = &out.evidence {
            let count_valid = s
                .compactions
                .is_some_and(|n| self.count.is_none_or(|p| n >= p));
            if count_valid {
                self.count = s.compactions;
                if self.baseline.is_none() {
                    self.baseline = self.count;
                }
                out.elapsed_compactions = self
                    .count
                    .zip(self.baseline)
                    .and_then(|(n, b)| n.checked_sub(b));
            } else {
                out.diagnostics
                    .push("missing or regressed compaction count".into());
            }
            let valid = s.members.each_ref().map(|m| {
                !m.identity.trim().is_empty()
                    && !m.revision.trim().is_empty()
                    && !m.checkpoint.trim().is_empty()
                    && !m.provenance.trim().is_empty()
                    && m.freshness.is_some_and(|t| t >= 0 && t <= clock.wall)
            });
            let trusted = delta.is_some() && clock.discontinuity.is_none();
            let mut advanced = [false; 2];
            for (i, m) in s.members.iter().enumerate() {
                if !valid[i] {
                    out.diagnostics
                        .push(format!("member {i}: missing, invalid or future evidence"));
                }
                if trusted && valid[i] {
                    let changed = self.evidence[i]
                        .as_ref()
                        .is_some_and(|p| p.identity != m.identity);
                    if changed {
                        out.diagnostics
                            .push(format!("member {i}: member identity changed"));
                    }
                    let advances = self.evidence[i]
                        .as_ref()
                        .is_none_or(|p| changed || m.freshness > p.freshness);
                    advanced[i] = self.evidence[i]
                        .as_ref()
                        .is_some_and(|p| m.freshness > p.freshness);
                    if advances {
                        self.ages[i] = m
                            .freshness
                            .and_then(|t| clock.wall.checked_sub(t))
                            .map(|a| a as u64);
                        self.evidence[i] = Some(m.clone());
                    }
                }
            }
            out.divergence = s.members[0]
                .freshness
                .zip(s.members[1].freshness)
                .filter(|_| valid.iter().all(|v| *v))
                .map(|(a, b)| a.abs_diff(b));
            let coherent = trusted
                && count_valid
                && valid.iter().all(|v| *v)
                && s.members[0].identity != s.members[1].identity
                && s.members[0].revision == s.members[1].revision
                && s.members[0].checkpoint == s.members[1].checkpoint
                && (0..2).all(|i| self.evidence[i].as_ref() == Some(&s.members[i]))
                && advanced.iter().all(|v| *v);
            if coherent {
                self.coherent = Some(s.clone());
                self.baseline = self.count;
                out.elapsed_compactions = Some(0);
                if self
                    .ages
                    .iter()
                    .all(|a| a.is_some_and(|a| a < config.maximum_age))
                    && out
                        .divergence
                        .is_some_and(|d| d <= config.divergence_tolerance)
                {
                    self.active = false;
                }
            }
        } else {
            out.diagnostics
                .push("missing or unparseable snapshot".into());
        }
        out.ages = if delta.is_some() {
            self.ages
        } else {
            [None; 2]
        };
        if self
            .ages
            .iter()
            .any(|a| a.is_some_and(|a| a >= config.maximum_age))
        {
            out.reasons.push("stale".into());
        }
        if out
            .divergence
            .is_some_and(|d| d > config.divergence_tolerance)
        {
            out.reasons.push("divergence".into());
        }
        if out
            .elapsed_compactions
            .is_some_and(|n| n >= config.compaction_cap)
        {
            out.reasons.push("compaction cap".into());
        }
        if !out.reasons.is_empty() && !self.active {
            self.episode += 1;
            self.active = true;
            out.new_alarm = true;
        }
        out.episode = self.active.then_some(self.episode);
        out
    }
}

#[cfg(test)]
#[path = "fleet_stale_pair_alert_tests.rs"]
mod tests;
