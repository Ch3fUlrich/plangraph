//! Pure family availability floor and stable plan ordering; no caller integration.
use crate::{
    health::{Health, State, UntilSrc},
    hosts::Role,
};
use std::collections::BTreeSet;

#[cfg(test)]
#[path = "health_family_tests.rs"]
mod tests;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegInfo {
    pub leg_key: String,
    pub family: String,
    pub tier: u32,
    pub route_key: String,
    pub model: String,
}

pub(crate) fn unique_plan(plan: &[LegInfo]) -> Vec<&LegInfo> {
    let mut seen = BTreeSet::new();
    plan.iter()
        .filter(|leg| seen.insert(&leg.leg_key))
        .collect()
}

fn health<'a>(healths: &'a [(String, Health)], key: &str) -> Option<&'a Health> {
    healths.iter().find(|(id, _)| id == key).map(|(_, h)| h)
}

fn healthy(h: Option<&Health>) -> bool {
    h.is_none_or(|h| matches!(h.state, State::Closed | State::Degraded))
}

/// Preserve evidence, flooring only the first eligible heuristic OPEN per family.
/// Missing health is Closed; metadata and candidates come only from the first plan entry.
pub fn family_floor(
    healths: &[(String, Health)],
    legs: &[LegInfo],
    _role: Role,
) -> Vec<(String, Health)> {
    let plan = unique_plan(legs);
    let mut out = healths.to_vec();
    let mut floored = BTreeSet::new();
    for leg in &plan {
        if floored.contains(&leg.family)
            || plan
                .iter()
                .any(|peer| peer.family == leg.family && healthy(health(healths, &peer.leg_key)))
        {
            continue;
        }
        let Some(h) = health(healths, &leg.leg_key) else {
            continue;
        };
        if h.state != State::Open
            || !h.heuristic
            || h.until_src == Some(UntilSrc::OmniRoute)
            || h.reason.is_some_and(|class| class.carries_reset())
        {
            continue;
        }
        if let Some((_, h)) = out.iter_mut().find(|(id, _)| id == &leg.leg_key) {
            h.state = State::Degraded;
            floored.insert(&leg.family);
        }
    }
    out
}

/// Stable tier-first ordering. OPEN filtering belongs to RL18, not this function.
pub fn order_legs(plan: &[LegInfo], healths: &[(String, Health)], role: Role) -> Vec<LegInfo> {
    let mut out = unique_plan(plan);
    out.sort_by_key(|leg| {
        let h = health(healths, &leg.leg_key);
        let rank = match h.map_or(State::Closed, |h| h.state) {
            State::Closed => 0,
            State::Degraded => 1,
            State::HalfOpen => 2,
            State::Open => 3,
        };
        (
            leg.tier,
            role == Role::Review && h.is_some_and(|h| h.review_unfit),
            rank,
        )
    });
    out.into_iter().cloned().collect()
}
