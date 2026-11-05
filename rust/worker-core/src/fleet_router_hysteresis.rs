//! Pure observations only; no clock, filesystem, delivery or work-control integration.
//! Window timestamps are wall seconds; progression additionally requires injected
//! monotonic cadence. Restore the whole evaluator to preserve replay watermarks.
use crate::fleet_router_signals::{Reading, Signal, Snapshot};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const NAMES: [&str; 6] = [
    "rolling_ctx",
    "compactions",
    "open_asks",
    "inbound_rate",
    "claims",
    "go_latency",
];
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub name: String,
    pub unit: String,
    pub cadence: u64,
    pub high: f64,
    pub low: f64,
    pub enter: u32,
    pub exit: u32,
    pub freshness: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u8,
    pub signals: Vec<Rule>,
}
impl Config {
    pub fn validate(&self) -> Result<(), &'static str> {
        let names: BTreeSet<_> = self.signals.iter().map(|r| r.name.as_str()).collect();
        if self.schema_version != 1
            || names.len() != self.signals.len()
            || names.len() != NAMES.len()
        {
            return Err("invalid schema or duplicate configuration");
        }
        if self.signals.iter().any(|r| {
            !NAMES.contains(&r.name.as_str())
                || r.unit.trim().is_empty()
                || r.cadence == 0
                || r.freshness == 0
                || r.enter == 0
                || r.exit == 0
                || !r.high.is_finite()
                || !r.low.is_finite()
                || r.low >= r.high
        }) {
            return Err("invalid signal rule");
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    last: Option<(i64, u64)>,
    entering: u32,
    exiting: u32,
    episode: u64,
    latched: bool,
}
impl State {
    fn reset(&mut self) {
        self.entering = 0;
        self.exiting = 0;
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Observation {
    Unknown(String),
    Overload(u64),
    Recovery(u64),
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Finding {
    pub signal: String,
    pub observation: Observation,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evaluator {
    config: Config,
    states: BTreeMap<String, State>,
}
impl Evaluator {
    pub fn new(config: Config) -> Result<Self, &'static str> {
        config.validate()?;
        Ok(Self {
            config,
            states: BTreeMap::new(),
        })
    }
    /// Configuration/state validation also runs after JSON restoration.
    /// `wall_jump` covers either direction. Unknown preserves the latched episode.
    pub fn evaluate(
        &mut self,
        snapshot: &Snapshot,
        mono: u64,
        wall_jump: bool,
    ) -> Result<Vec<Finding>, &'static str> {
        self.config.validate()?;
        if self
            .states
            .keys()
            .any(|k| !self.config.signals.iter().any(|r| &r.name == k))
        {
            return Err("unconfigured state");
        }
        let mut findings = Vec::new();
        for rule in &self.config.signals {
            let state = self.states.entry(rule.name.clone()).or_default();
            let mut matches = snapshot.signals.iter().filter(|s| s.name == rule.name);
            let signal = matches.next();
            let value = if snapshot.schema_version != 1 {
                Err("invalid snapshot schema".into())
            } else if wall_jump {
                Err("wall discontinuity".into())
            } else if matches.next().is_some() {
                Err("duplicate signal".into())
            } else {
                reading(signal, rule, snapshot.timestamp)
            };
            let end = snapshot.end_inclusive;
            let interval = snapshot.start_exclusive.and_then(|s| end.checked_sub(s));
            let valid_window = interval.and_then(|d| u64::try_from(d).ok()) == Some(rule.cadence)
                && end <= snapshot.timestamp;
            let value = value.and_then(|v| {
                if !valid_window {
                    Err("invalid window".into())
                } else {
                    Ok(v)
                }
            });
            if let Err(reason) = &value {
                state.reset();
                // Only structurally valid windows may consume replay watermarks.
                // Rejected timestamps/monotonic inputs must not poison future progress.
                if valid_window && snapshot.schema_version == 1 && !wall_jump {
                    state.last = Some(
                        state
                            .last
                            .map_or((end, mono), |(e, m)| (e.max(end), m.max(mono))),
                    );
                }
                findings.push(Finding {
                    signal: rule.name.clone(),
                    observation: Observation::Unknown(reason.clone()),
                });
                continue;
            }
            if let Some((last_end, last_mono)) = state.last {
                if end <= last_end {
                    continue;
                }
                if mono <= last_mono {
                    state.reset();
                    findings.push(Finding {
                        signal: rule.name.clone(),
                        observation: Observation::Unknown("monotonic discontinuity".into()),
                    });
                    continue;
                }
                if end
                    .checked_sub(last_end)
                    .and_then(|d| u64::try_from(d).ok())
                    != Some(rule.cadence)
                    || mono.checked_sub(last_mono) != Some(rule.cadence)
                {
                    state.reset();
                }
            }
            state.last = Some((end, mono));
            let value = value.expect("known reading");
            let observation = if !state.latched && value >= rule.high {
                state.entering = state.entering.saturating_add(1);
                if state.entering >= rule.enter {
                    let Some(episode) = state.episode.checked_add(1) else {
                        state.reset();
                        findings.push(Finding {
                            signal: rule.name.clone(),
                            observation: Observation::Unknown("episode exhausted".into()),
                        });
                        continue;
                    };
                    state.episode = episode;
                    state.latched = true;
                    state.reset();
                    Some(Observation::Overload(state.episode))
                } else {
                    None
                }
            } else if state.latched && value <= rule.low {
                state.exiting = state.exiting.saturating_add(1);
                if state.exiting >= rule.exit {
                    state.latched = false;
                    state.reset();
                    Some(Observation::Recovery(state.episode))
                } else {
                    None
                }
            } else {
                state.reset();
                None
            };
            if let Some(observation) = observation {
                findings.push(Finding {
                    signal: rule.name.clone(),
                    observation,
                });
            }
        }
        Ok(findings)
    }
}
fn reading(signal: Option<&Signal>, rule: &Rule, now: i64) -> Result<f64, String> {
    let s = signal.ok_or("absent signal")?;
    if s.unit != rule.unit {
        return Err("wrong unit".into());
    }
    let observed = s.observed_at.ok_or("absent observation time")?;
    let age = now
        .checked_sub(observed)
        .ok_or("invalid observation time")?;
    if age < 0 {
        return Err("future reading".into());
    }
    if age as u64 > rule.freshness {
        return Err("stale reading".into());
    }
    match &s.value {
        Reading::Known(v) if v.is_finite() => Ok(*v),
        Reading::Known(_) => Err("nonfinite reading".into()),
        Reading::Unknown(reason) => Err(format!("unknown: {reason}")),
    }
}
#[cfg(test)]
#[path = "fleet_router_hysteresis_tests.rs"]
mod tests;
