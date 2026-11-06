//! File-only caller. Persist the entire state after every tick, including failed
//! sends. Slot time is for evaluation only; delivery uses the tick's saved epoch.
use crate::{
    fleet_alarm_delivery as delivery, fleet_router_hysteresis as router,
    fleet_router_signals::acquire,
};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, fs, path::PathBuf};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub tick_seconds: u64,
    pub rules: router::Config,
    pub sources: [PathBuf; 3],
    pub retry_delay: u64,
    pub maximum_attempts: u32,
    pub silence_after: u64,
}
impl Config {
    pub fn validate(&self) -> Result<(), &'static str> {
        self.rules.validate()?;
        if self.tick_seconds == 0
            || self.tick_seconds > i64::MAX as u64
            || self
                .rules
                .signals
                .iter()
                .any(|r| r.cadence != self.tick_seconds)
        {
            return Err("invalid tick cadence");
        }
        delivery::HeartbeatMonitor::new(
            "hysteresis".into(),
            0,
            self.silence_after,
            self.retry_delay,
            self.maximum_attempts,
        )?;
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
pub struct State {
    rules: router::Config,
    evaluator: router::Evaluator,
    slot: Option<i64>,
    mono: u64,
    active: BTreeSet<String>,
    deliveries: delivery::Deliveries,
    silence: delivery::HeartbeatMonitor,
}
#[allow(clippy::too_many_arguments)]
pub fn tick(
    c: &Config,
    saved: &mut Option<State>,
    wall: i64,
    now: u64,
    jump: bool,
    acks: &BTreeSet<String>,
    heartbeat: Option<u64>,
    sink: &mut impl FnMut(&str) -> Result<(), String>,
    opened: &mut Vec<String>,
    diagnostics: &mut Vec<String>,
) -> Result<Vec<router::Finding>, String> {
    if saved.is_none() {
        *saved = Some(State {
            rules: c.rules.clone(),
            evaluator: router::Evaluator::new(c.rules.clone())?,
            slot: None,
            mono: 0,
            active: BTreeSet::new(),
            deliveries: delivery::Deliveries::new(c.retry_delay, c.maximum_attempts)?,
            silence: delivery::HeartbeatMonitor::new(
                "hysteresis".into(),
                now,
                c.silence_after,
                c.retry_delay,
                c.maximum_attempts,
            )?,
        });
    }
    let s = saved.as_mut().ok_or("missing hysteresis state")?;
    let slot = wall.div_euclid(c.tick_seconds as i64);
    let mut findings = Vec::new();
    if serde_json::to_value(&s.rules).map_err(|_| "rules encode")?
        != serde_json::to_value(&c.rules).map_err(|_| "rules encode")?
    {
        diagnostics.push("hysteresis rules changed".into());
    } else if s.slot != Some(slot) || jump {
        let elapsed = s
            .slot
            .and_then(|p| slot.checked_sub(p))
            .and_then(|d| u64::try_from(d).ok())
            .unwrap_or(0);
        s.mono = s
            .mono
            .saturating_add(elapsed.saturating_mul(c.tick_seconds));
        s.slot = Some(slot);
        let evaluation = (|| {
            let bytes = c.sources.each_ref().map(fs::read);
            let inputs = bytes
                .each_ref()
                .map(|r| r.as_deref().map_err(|_| "source read failed"));
            let names = c.sources.each_ref().map(|p| p.to_str().unwrap_or("source"));
            let end = slot
                .checked_mul(c.tick_seconds as i64)
                .ok_or("slot overflow")?;
            let snapshot = acquire(inputs, names, end, c.tick_seconds as i64);
            s.evaluator.evaluate(&snapshot, s.mono, jump)
        })();
        match evaluation {
            Ok(result) => findings = result,
            Err(error) => diagnostics.push(error.into()),
        }
        for f in &findings {
            if !matches!(f.observation, router::Observation::Overload(_)) {
                let prefix = format!("hysteresis:{}:", f.signal);
                s.active.retain(|id| !id.starts_with(&prefix));
            }
            if let router::Observation::Overload(n) = f.observation {
                let id = format!("hysteresis:{}:{n}", f.signal);
                if s.active.insert(id.clone()) {
                    opened.push(id);
                }
            }
        }
    }
    s.active.retain(|id| {
        if acks.contains(id) {
            s.deliveries.acknowledge(id);
            false
        } else {
            true
        }
    });
    for id in &s.active {
        s.deliveries.tick(id, now, sink);
    }
    s.silence.tick(heartbeat, now, sink);
    Ok(findings)
}

#[cfg(test)]
#[path = "fleet_foundation_hysteresis_tests.rs"]
mod tests;
