//! First-attempt served-route/model rates. Inputs must come from authenticated records.
use crate::{
    health::{Health, State},
    health_family::{unique_plan, LegInfo},
};
use std::collections::BTreeMap;

#[cfg(test)]
#[path = "health_rates_tests.rs"]
mod tests;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FirstAttempt {
    pub run_id: String,
    pub attempt: u32,
    pub route_key: String,
    pub model: String,
    pub usable: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RatePolicy {
    pub min_samples: usize,
    pub min_usable_percent: u8,
}

impl Default for RatePolicy {
    fn default() -> Self {
        Self {
            min_samples: 20,
            min_usable_percent: 50,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RateError {
    InvalidPolicy,
}

/// Values are (samples, usable samples, demote). Last physical correction wins.
#[allow(clippy::type_complexity)] // Public bridge signature specified by RL11.
pub fn usable_rates(
    rows: &[FirstAttempt],
    policy: RatePolicy,
) -> Result<BTreeMap<(String, String), (usize, usize, bool)>, RateError> {
    if policy.min_samples == 0 || policy.min_usable_percent > 100 {
        return Err(RateError::InvalidPolicy);
    }
    let mut attempts = BTreeMap::new();
    for row in rows.iter().filter(|row| row.attempt == 1) {
        attempts.insert((&row.run_id, &row.route_key, &row.model), row.usable);
    }
    let mut rates = BTreeMap::new();
    for ((_, route, model), usable) in attempts {
        let entry = rates
            .entry((route.clone(), model.clone()))
            .or_insert((0usize, 0usize, false));
        entry.0 += 1;
        entry.1 += usize::from(usable);
    }
    for (samples, usable, demote) in rates.values_mut() {
        *demote = *samples >= policy.min_samples
            && (*usable as u128) * 100 < (*samples as u128) * u128::from(policy.min_usable_percent);
    }
    Ok(rates)
}

/// Rate evidence changes ordering only: no expiry, OPEN, or hard-fact revival.
pub fn apply_rates(
    legs: &[LegInfo],
    healths: &mut [(String, Health)],
    rates: &BTreeMap<(String, String), (usize, usize, bool)>,
) {
    let plan = unique_plan(legs);
    for (key, h) in healths {
        if h.state != State::Closed {
            continue;
        }
        if let Some(leg) = plan.iter().find(|leg| &leg.leg_key == key) {
            if rates
                .get(&(leg.route_key.clone(), leg.model.clone()))
                .is_some_and(|rate| rate.2)
            {
                h.state = State::Degraded;
            }
        }
    }
}
