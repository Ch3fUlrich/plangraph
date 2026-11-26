//! Optional watchdog conditions and readiness facts.
use super::{derive, index, Evidence, IdleEvidence, Keeper, Ready};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn keeper_lanes(now: u64, keeper: &Keeper) -> Evidence<Value> {
    let mut lapsed = Vec::new();
    let mut pending = BTreeMap::<&str, u64>::new();
    for item in index(keeper)?.into_values() {
        let owner = item
            .owner
            .as_deref()
            .filter(|owner| !owner.is_empty())
            .unwrap_or("unassigned");
        if matches!(item.state.as_str(), "open" | "claimed" | "in_review") {
            *pending.entry(owner).or_default() += 1;
        }
        if item.state != "claimed" {
            continue;
        }
        let Some(lease) = item.lease_until.as_deref() else {
            continue;
        };
        let over = now as f64 - derive::parse_time(lease)?;
        if over > 0.0 {
            lapsed.push(
                json!({"item":item.key,"owner":item.claimed_by.as_deref().unwrap_or(owner),
                               "age_minutes":(over / 60.0) as u64,"open_lane":true}),
            );
        }
    }
    Ok(json!({"pending_by_role":pending,"lapsed_leases":lapsed}))
}

pub(super) fn ready(keeper: &Keeper, ready: &Ready) -> Evidence<Value> {
    let items = index(keeper)?;
    let mut roles = BTreeMap::<&str, BTreeSet<&str>>::new();
    for row in &ready.items {
        let item = items
            .get(row.key.as_str())
            .ok_or("ready response disagrees with Keeper snapshot")?;
        if item.state != "open"
            || item
                .claimed_by
                .as_deref()
                .is_some_and(|name| !name.is_empty())
        {
            return Err("ready response disagrees with Keeper snapshot".into());
        }
        let (family, _) = item
            .key
            .split_once(':')
            .ok_or("ready item has no project family")?;
        let owner = item
            .owner
            .as_deref()
            .filter(|owner| !owner.is_empty())
            .unwrap_or("unassigned");
        roles.entry(owner).or_default().insert(family);
    }
    Ok(json!({"ready_families":roles}))
}

pub(super) fn idle_capacity(now: u64, evidence: IdleEvidence) -> Evidence<Value> {
    if !derive::current(now, evidence.observed_at) {
        return Err("typed host-idle resource observation is outside the current window".into());
    }
    // The watchdog's shared evaluator owns the resource and readiness schema.
    let snapshot: crate::host_idle::Snapshot = serde_json::from_value(evidence.snapshot.clone())
        .map_err(|error| format!("invalid typed host-idle resource observation: {error}"))?;
    snapshot.messages()?;
    Ok(json!({"host_idle_capacity":evidence.snapshot}))
}
