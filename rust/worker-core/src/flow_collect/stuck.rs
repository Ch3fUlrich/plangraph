//! Stuck-lane rows and ready families, from Keeper's own readiness and lease facts.
use super::{derive::parse_time, Item, Readings, SERIES_MAX_AGE_S};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

type Fields = Result<Map<String, Value>, String>;

/// Dependencies as the keeper stores them: a list, a JSON list in text, or a separated string.
pub fn deps(item: &Item) -> Vec<String> {
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

/// Keeper's agent-ready result (landing-train parts and operator steps already decided),
/// restricted to items still open and unclaimed in the same snapshot.
fn ready<'a>(r: &Readings, items: &'a [Item]) -> Result<Vec<&'a Item>, String> {
    let keys = r.ready_keys.as_ref().ok_or("keeper ready reading failed")?;
    Ok(items
        .iter()
        .filter(|i| {
            i.state == "open"
                && i.claimed_by.as_deref().is_none_or(str::is_empty)
                && keys.contains(&i.key)
        })
        .collect())
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

/// Leases that ran out on claimed items, and merged items still in review.
pub fn rows(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
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
    let mut merged = Vec::new();
    for item in items.iter().filter(|i| i.state == "in_review" && i.merged) {
        merged.push(json!({"item":item.key,"owner":owner(item),
                           "age_minutes":minutes(r.now, &item.updated_at, &item.key)?,"in_review":true}));
    }
    let mut out = Map::new();
    out.insert("lapsed_leases".into(), json!(lapsed));
    out.insert("merged_reviews".into(), json!(merged));
    Ok(out)
}

/// Ready, owned items whose owner has no session in a current, reachable fleet sample.
pub fn ready_owners(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let series = r.series.as_ref().ok_or("fleet series reading failed")?;
    let current: Vec<_> = series
        .iter()
        .filter(|s| s.reachable && r.now - s.ts <= SERIES_MAX_AGE_S)
        .collect();
    if current.is_empty() {
        return Err("no current fleet sample to tell who has a session".into());
    }
    let live: BTreeSet<&str> = current
        .iter()
        .flat_map(|s| s.sessions.iter().map(String::as_str))
        .collect();
    let alive = |owner: &str| {
        live.iter()
            .any(|s| *s == owner || s.starts_with(&format!("{owner}-")))
    };
    let mut rows = Vec::new();
    for item in ready(r, items)?
        .into_iter()
        .filter(|i| i.owner.as_deref().is_some_and(|o| !o.is_empty()))
    {
        rows.push(json!({"item":item.key,"owner":owner(item),
                         "age_minutes":minutes(r.now, &item.updated_at, &item.key)?,
                         "no_live_session":!alive(&owner(item))}));
    }
    let mut out = Map::new();
    out.insert("ready_owners".into(), json!(rows));
    Ok(out)
}

/// Ready work per role: the project families that have a ready item for that owner.
pub fn families(r: &Readings) -> Fields {
    let items = r.items.as_ref().ok_or("keeper items reading failed")?;
    let mut roles: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for item in ready(r, items)? {
        let family = item.key.split(':').next().unwrap_or(&item.key).to_owned();
        roles.entry(owner(item)).or_default().insert(family);
    }
    let mut out = Map::new();
    out.insert("ready_families".into(), json!(roles));
    Ok(out)
}
