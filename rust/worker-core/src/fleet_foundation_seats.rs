//! File-only control. Save the entire state before forwarding collected requests.
//! Failed request appends consume attempts; requests are never executed here.
use crate::{
    fleet_alarm_delivery as alarm, fleet_seat_probe as probe, fleet_seat_supply as supply,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeSet, fs, path::PathBuf};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    daily_attempts: u32,
    timeout_s: u32,
    retry_cap: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    policy: supply::Policy,
    seats: Vec<supply::Seat>,
    ruling: Option<supply::Ruling>,
    catalog_path: PathBuf,
    limits: Limits,
    authority: Option<probe::Authority>,
    retry_delay: u64,
    maximum_attempts: u32,
    silence_after: u64,
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        self.initial(0).map(|_| ())
    }
    fn initial(&self, now: u64) -> Result<State, String> {
        Ok(State {
            probe: probe::State::default(),
            counted: None,
            active: BTreeSet::new(),
            deliveries: alarm::Deliveries::new(self.retry_delay, self.maximum_attempts)?,
            silence: alarm::HeartbeatMonitor::new(
                "seat-supply".into(),
                now,
                self.silence_after,
                self.retry_delay,
                self.maximum_attempts,
            )?,
        })
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct State {
    probe: probe::State,
    counted: Option<supply::State>,
    active: BTreeSet<String>,
    deliveries: alarm::Deliveries,
    silence: alarm::HeartbeatMonitor,
}
#[allow(clippy::too_many_arguments)]
pub fn tick(
    c: &Config,
    saved: &mut Option<State>,
    wall: i64,
    epoch_ms: u64,
    acks: &BTreeSet<String>,
    heartbeat: Option<u64>,
    sink: &mut impl FnMut(&str) -> Result<(), String>,
    opened: &mut Vec<String>,
) -> Result<(Value, Vec<probe::Request>), String> {
    let now = epoch_ms / 1000;
    let mut s = match saved.as_ref() {
        Some(s) => s.clone(),
        None => c.initial(now)?,
    };
    if s.counted
        .as_ref()
        .is_some_and(|v| epoch_ms < v.monotonic_ms)
    {
        return Err("seat control clock backwards".into());
    }
    let input = supply::Input {
        epoch_day: wall.div_euclid(86400),
        now_s: wall,
        monotonic_ms: epoch_ms,
        previous_monotonic_ms: s.counted.as_ref().map_or(0, |v| v.monotonic_ms),
        catalog_bytes: fs::read(&c.catalog_path).unwrap_or_default(),
        seats: c.seats.clone(),
        ruling: c.ruling.clone(),
    };
    let counted = supply::count(&c.policy, &input);
    let id = format!("seat-supply:{}:{}", counted.sha, counted.epoch_day);
    s.active.retain(|_| counted.hold);
    if counted.hold && s.deliveries.record(&id).is_none() && s.active.insert(id.clone()) {
        opened.push(id);
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
    let mut requests = Vec::new();
    let records = probe::plan(
        &mut s.probe,
        &c.policy,
        &input,
        &probe::Limits {
            daily_attempts: c.limits.daily_attempts,
            timeout_s: c.limits.timeout_s,
            retry_cap: c.limits.retry_cap,
        },
        c.authority.as_ref(),
        &mut |r| requests.push(r),
    )
    .iter()
    .map(|r| format!("{r:?}"))
    .collect::<Vec<_>>();
    let summary = json!({"count":counted,"records":records});
    s.counted = Some(counted);
    *saved = Some(s);
    Ok((summary, requests))
}

#[cfg(test)]
#[path = "fleet_foundation_seats_tests.rs"]
mod tests;
