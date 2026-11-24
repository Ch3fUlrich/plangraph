//! Stuck-lane rows and ready families: what waits although the fleet could take it.
use super::{derive::open, derive::parse_time, Item, Readings};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

type Fields = Result<Map<String, Value>, String>;

fn deps(item: &Item) -> Vec<String> {
    match &item.depends_on {
        Value::Array(rows) => rows
            .iter()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect(),
        Value::String(text) => serde_json::from_str::<Vec<String>>(text).unwrap_or_else(|_| {
            text.split(|c: char| c == ',' || c.is_whitespace())
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect()
        }),
        _ => Vec::new(),
    }
}

/// Open, unclaimed, not waiting on the operator, and every dependency finished.
fn ready(items: &[Item]) -> Vec<&Item> {
    let closed: BTreeSet<&str> = items
        .iter()
        .filter(|i| !open(i) && i.state != "cancelled")
        .map(|i| i.key.as_str())
        .collect();
    items
        .iter()
        .filter(|i| {
            i.state == "open"
                && !i.waits_on_operator
                && i.claimed_by.as_deref().is_none_or(str::is_empty)
                && deps(i).iter().all(|d| closed.contains(d.as_str()))
        })
        .collect()
}

fn owner(item: &Item) -> String {
    item.owner
        .clone()
        .filter(|o| !o.is_empty())
        .unwrap_or_else(|| "unassigned".into())
}

fn minutes(now: f64, since: &Option<String>, key: &str) -> Result<u64, String> {
    let at = parse_time(
        since
            .as_deref()
            .ok_or_else(|| format!("{key} has no timestamp"))?,
    )?;
    Ok(((now - at).max(0.0) / 60.0) as u64)
}

pub fn rows(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let series = r.series.as_ref().ok_or("fleet series reading failed")?;
    let live: BTreeSet<&str> = series
        .iter()
        .flat_map(|s| s.sessions.iter().map(String::as_str))
        .collect();
    let alive = |owner: &str| {
        live.iter()
            .any(|s| *s == owner || s.starts_with(&format!("{owner}-")))
    };
    let waiting = ready(items);
    let mut lapsed = Vec::new();
    for item in items.iter().filter(|i| i.state == "claimed") {
        // A claim without a lease (legacy or reaped rows) has nothing to lapse.
        let Some(lease) = item.lease_until.as_deref() else {
            continue;
        };
        let over = r.now - parse_time(lease)?;
        if over > 0.0 {
            let who = item.claimed_by.clone().unwrap_or_else(|| owner(item));
            lapsed.push(json!({"item":item.key,"owner":who,"age_minutes":(over / 60.0) as u64,"open_lane":true}));
        }
    }
    let mut ordered = Vec::new();
    let mut no_session = Vec::new();
    for item in waiting
        .iter()
        .filter(|i| i.owner.as_deref().is_some_and(|o| !o.is_empty()))
    {
        let age = minutes(r.now, &item.updated_at, &item.key)?;
        ordered
            .push(json!({"item":item.key,"owner":owner(item),"age_minutes":age,"unclaimed":true}));
        no_session.push(
            json!({"item":item.key,"owner":owner(item),"age_minutes":age,
                               "no_live_session":!alive(&owner(item))}),
        );
    }
    let mut oldest: Option<(u64, &str)> = None;
    for item in &waiting {
        let age = minutes(r.now, &item.updated_at, &item.key)?;
        if oldest.is_none_or(|(best, _)| age > best) {
            oldest = Some((age, item.key.as_str()));
        }
    }
    let idle: Vec<Value> = oldest
        .into_iter()
        .flat_map(|(age, key)| {
            series.iter().filter(|s| s.reachable && s.runs == 0 && s.ready > 0).map(move |s| {
            json!({"item":key,"owner":s.host,"age_minutes":age,"capacity_available":true})
        })
        })
        .collect();
    let mut merged = Vec::new();
    for item in items.iter().filter(|i| i.state == "in_review" && i.merged) {
        merged.push(json!({"item":item.key,"owner":owner(item),
                           "age_minutes":minutes(r.now, &item.updated_at, &item.key)?,"in_review":true}));
    }
    let mut out = Map::new();
    out.insert("lapsed_leases".into(), json!(lapsed));
    out.insert("ordered_items".into(), json!(ordered));
    out.insert("ready_owners".into(), json!(no_session));
    out.insert("idle_capacity".into(), json!(idle));
    out.insert("merged_reviews".into(), json!(merged));
    Ok(out)
}

/// Ready work per role: the project families that have a ready item for that owner.
pub fn families(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let mut roles: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for item in ready(items) {
        let family = item.key.split(':').next().unwrap_or(&item.key).to_owned();
        roles.entry(owner(item)).or_default().insert(family);
    }
    let mut out = Map::new();
    out.insert("ready_families".into(), json!(roles));
    Ok(out)
}
