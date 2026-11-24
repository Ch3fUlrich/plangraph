//! RL18a: lays OmniRoute provider state and our evidence over the adapter plan.
//!
//! The adapter lists the legs; this only reorders inside a tier (`order_legs`) and
//! drops OPEN legs. It adds no leg, never drops the last one, and under `shadow`
//! only reports. Switch `health`: off (default), shadow, on; `on` needs `route_gate` on.

use crate::provider_state::{Http, Reader};
use pg_worker_core::health::{family_floor, order_legs, LegInfo, Row, State};
use pg_worker_core::{health_until::fold_with_provider, health_until::ProviderState, hosts::Role};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mode {
    Off,
    Shadow,
    On,
}

/// `on` is refused (stays off) until the route gate is on: single-flight probes need its slot.
pub(crate) fn mode(env: Option<&str>, gate_env: Option<&str>, path: &Path, role: &str) -> Mode {
    let get = |env: Option<&str>, key| {
        env.map(str::to_owned)
            .or_else(|| crate::leg_ref::switch_value(path, key, role))
    };
    match get(env, "health").as_deref().unwrap_or("off") {
        "shadow" => Mode::Shadow,
        "on" if get(gate_env, "route_gate").as_deref() == Some("on") => Mode::On,
        "on" => {
            eprintln!("health: on refused while route_gate is not on; off");
            Mode::Off
        }
        _ => Mode::Off,
    }
}

type States = BTreeMap<String, ProviderState>;

fn leg_key(leg: &Value) -> Option<String> {
    let model = leg["model"].as_str()?;
    Some(match leg["leg_key"].as_str() {
        Some(key) => key.to_owned(),
        None if leg["harness"] == "agy" && !model.contains('/') => format!("agy/{model}"),
        None => model.to_owned(),
    })
}

/// Apply the overlay to `plan["legs"]`; returns the report, or `None` when it cannot read the plan.
pub(crate) fn apply(
    plan: &mut Value,
    mode: Mode,
    role: Role,
    rows: &[Row],
    states: Option<&States>,
    now: i64,
) -> Option<Value> {
    if mode == Mode::Off {
        return None;
    }
    let legs = plan["legs"].as_array()?.clone();
    let (mut infos, mut healths, mut tier, mut last) = (vec![], vec![], 0, "");
    for leg in &legs {
        let (id, family) = (leg["id"].as_str()?, leg["family"].as_str()?);
        let key = leg_key(leg)?;
        let route = leg["route_key"].as_str().unwrap_or(&key);
        // Tiers are the adapter's own runs: reordering never crosses one.
        let name = leg["tier"].as_str().unwrap_or("");
        if !infos.is_empty() && name != last {
            tier += 1;
        }
        last = name;
        healths.push((
            id.to_owned(),
            fold_with_provider(
                &key,
                route,
                role,
                rows,
                now,
                states.and_then(|s| s.get(&key)),
            ),
        ));
        infos.push(LegInfo {
            leg_key: id.to_owned(),
            family: family.to_owned(),
            tier,
            route_key: route.to_owned(),
            model: leg["model"].as_str()?.to_owned(),
        });
    }
    let healths = family_floor(&healths, &infos, role);
    let open = |id: &str| {
        healths
            .iter()
            .any(|(k, h)| k == id && h.state == State::Open)
    };
    let ordered = order_legs(&infos, &healths, role);
    let kept: Vec<&str> = ordered
        .iter()
        .map(|l| l.leg_key.as_str())
        .filter(|id| !open(id))
        .collect();
    let original: Vec<&str> = infos.iter().map(|l| l.leg_key.as_str()).collect();
    let dropped: Vec<&str> = original.iter().copied().filter(|id| open(id)).collect();
    let all_open = kept.is_empty();
    let before: Vec<&str> = original.iter().copied().filter(|id| !open(id)).collect();
    let report = json!({"mode": format!("{mode:?}").to_lowercase(),
        "would_reorder": !all_open && kept != before,
        "would_drop": if all_open { vec![] } else { dropped.clone() },
        "all_open": all_open});
    if mode == Mode::On && !all_open {
        let by_id = |id: &str| legs.iter().find(|l| l["id"] == id).cloned().unwrap();
        plan["legs"] = json!(kept.iter().map(|id| by_id(id)).collect::<Vec<_>>());
        if plan["first_leg"].is_object() {
            plan["first_leg"] = plan["legs"][0].clone();
        }
        if let Some(skipped) = plan["skipped"].as_array_mut() {
            skipped.extend(
                dropped
                    .iter()
                    .map(|id| json!({"leg": id, "reason": "omniroute_open"})),
            );
        }
    }
    plan["health_overlay"] = report.clone();
    Some(report)
}

static READER: Mutex<Option<Reader<Http>>> = Mutex::new(None);

pub(crate) fn configured(role: &str) -> Mode {
    let env = |name| std::env::var(name).ok();
    mode(
        env("PLANGRAPH_RELIABILITY_HEALTH").as_deref(),
        env("PLANGRAPH_RELIABILITY_ROUTE_GATE").as_deref(),
        &crate::prompt::config_root().join("reliability.json"),
        role,
    )
}

/// Production entry: configured switch, evidence rows and the shared 60 s reader.
pub(crate) fn run(state: &Path, role: &str, plan: &mut Value) {
    let mode = configured(role);
    let env = |name| std::env::var(name).ok();
    let Ok(role) = serde_json::from_value::<Role>(json!(role)) else {
        return;
    };
    if mode == Mode::Off {
        return;
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    // A broken evidence store must not stop routing: fall back to provider state alone.
    let rows: Vec<Row> = crate::health_report_rows::read_rows(state)
        .map(|evidence| {
            crate::health_report_rows::corrected(&crate::records::list_runs(state), &evidence)
                .iter()
                .filter_map(crate::health_report::health_row)
                .collect()
        })
        .unwrap_or_default();
    let mut guard = READER.lock().unwrap_or_else(|e| e.into_inner());
    if guard.is_none() {
        *guard = env("OMNIROUTE_BASE_URL")
            .map(|url| Reader::new(Http::new(&url, env("OMNIROUTE_API_KEY"))));
    }
    let states = guard.as_mut().and_then(|r| r.read(now)).cloned();
    apply(plan, mode, role, &rows, states.as_ref(), now);
}

#[cfg(test)]
#[path = "overlay_tests.rs"]
mod tests;
