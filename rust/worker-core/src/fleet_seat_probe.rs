//! Inactive daily planning only. Persist State before forwarding recorded requests.
//! Authority covers this plan only, never later spending or access acquisition.
use crate::fleet_seat_supply::{count, Input, Policy, Probe, Seat};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct State {
    pub day: Option<i64>,
    pub clock: i64,
    pub monotonic_ms: u64,
    pub attempts: Vec<Request>,
    pub retained: BTreeMap<String, Seat>,
    pub completed: BTreeSet<u32>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Authority {
    pub reference: String,
    pub day: i64,
    pub budget: u32,
    pub timeout_s: u32,
    pub retry_cap: u32,
    /// Exact canonical-seat actions with existing access, not permission to acquire it.
    pub existing_probe_access: BTreeSet<String>,
    pub may_record_not_needed: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Request {
    pub day: i64,
    pub attempt: u32,
    pub seat: Seat,
    pub timeout_s: u32,
    pub retry_cap: u32,
    pub authorized: bool,
    pub authority_reference: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Record {
    RequiresOperatorAuthorization,
    NotNeeded {
        authority_reference: String,
        reason: String,
    },
    Finding(&'static str),
}
pub struct Limits {
    pub daily_attempts: u32,
    pub timeout_s: u32,
    pub retry_cap: u32,
}
#[derive(Clone, Copy)]
pub enum Outcome {
    Success,
    Void,
    Timeout,
}
/// Results must reference a recorded current-day request. Replays cannot rewrite it.
pub fn complete(state: &mut State, attempt: u32, outcome: Outcome) -> bool {
    let Some(request) = state.attempts.iter().find(|r| r.attempt == attempt) else {
        return false;
    };
    if !state.completed.insert(attempt) {
        return false;
    }
    let mut seat = request.seat.clone();
    seat.probe = Probe {
        successful: matches!(outcome, Outcome::Success),
        void: matches!(outcome, Outcome::Void),
        timeout: matches!(outcome, Outcome::Timeout),
        authorization_reference: request.authority_reference.clone(),
    };
    state.retained.insert(seat.canonical.clone(), seat);
    true
}
/// No clock reads, filesystem, subprocess, network, spending or access mutations.
/// The sink records only; the caller owns durable storage and any later execution.
pub fn plan(
    state: &mut State,
    policy: &Policy,
    input: &Input,
    limits: &Limits,
    authority: Option<&Authority>,
    sink: &mut impl FnMut(Request),
) -> Vec<Record> {
    let mut records = vec![];
    if input.now_s.div_euclid(86400) != input.epoch_day
        || input.monotonic_ms < input.previous_monotonic_ms
        || state.day.is_some_and(|d| {
            input.epoch_day < d
                || input.now_s < state.clock
                || input.monotonic_ms < state.monotonic_ms
        })
    {
        return vec![Record::Finding("clock_backwards_or_invalid")];
    }
    if limits.daily_attempts == 0 || limits.timeout_s == 0 {
        return vec![Record::Finding("invalid_limits")];
    }
    if state.day != Some(input.epoch_day) {
        state.day = Some(input.epoch_day);
        state.attempts.clear();
        state.completed.clear();
    }
    state.clock = input.now_s;
    state.monotonic_ms = input.monotonic_ms;
    let covered = authority.filter(|a| {
        !a.reference.trim().is_empty()
            && a.day == input.epoch_day
            && a.budget >= limits.daily_attempts
            && a.timeout_s >= limits.timeout_s
            && a.retry_cap >= limits.retry_cap
    });
    let mut current = input.clone();
    for seat in &mut current.seats {
        if let Some(old) = state.retained.get(&seat.canonical) {
            if old.route_family == seat.route_family && old.proving_run == seat.proving_run {
                seat.probe = old.probe.clone();
            }
        }
    }
    if !count(policy, &current).hold {
        return match covered.filter(|a| a.may_record_not_needed) {
            Some(a) => vec![Record::NotNeeded {
                authority_reference: a.reference.clone(),
                reason: "validated supply meets caller quorum; no execution authorized".into(),
            }],
            None => vec![Record::RequiresOperatorAuthorization],
        };
    }
    // Validate candidates with hypothetical success through the existing contract.
    let mut candidates = input.clone();
    for seat in &mut candidates.seats {
        seat.probe = Probe {
            successful: true,
            void: false,
            timeout: false,
            authorization_reference: "candidate-validation-only".into(),
        };
    }
    let validated = count(policy, &candidates);
    let mut families: BTreeSet<_> = state
        .attempts
        .iter()
        .filter_map(|r| r.seat.route_family.clone())
        .collect();
    let retry = state.attempts.last().is_some_and(|r| {
        state.completed.contains(&r.attempt)
            && state
                .retained
                .get(&r.seat.canonical)
                .is_some_and(|s| s.probe.void)
    });
    if !state.attempts.is_empty() && !retry {
        return records;
    }
    if state.attempts.len() >= limits.daily_attempts as usize
        || (retry && state.attempts.len() > limits.retry_cap as usize)
    {
        return vec![Record::Finding("attempt_or_retry_limit")];
    }
    for reading in validated.readings {
        let seat = reading.seat;
        if !reading.eligible || families.contains(seat.route_family.as_ref().unwrap()) {
            continue;
        }
        let Some(a) = covered.filter(|a| a.existing_probe_access.contains(&seat.canonical)) else {
            records.push(Record::RequiresOperatorAuthorization);
            continue;
        };
        let request = Request {
            day: input.epoch_day,
            attempt: state.attempts.len() as u32 + 1,
            seat,
            timeout_s: limits.timeout_s,
            retry_cap: limits.retry_cap,
            authorized: true,
            authority_reference: a.reference.clone(),
        };
        families.insert(request.seat.route_family.clone().unwrap());
        state.attempts.push(request.clone());
        sink(request);
        return records;
    }
    records.push(Record::Finding("eligible_family_unavailable"));
    records
}

#[cfg(test)]
#[path = "fleet_seat_probe_tests.rs"]
mod tests;
