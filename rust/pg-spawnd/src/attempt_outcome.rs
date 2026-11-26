//! One keeper row describes one terminal spawner run, regardless of leg fallthrough.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeMap, io::Write};

// The watchdog settles this input-specific failure. CLI and I/O errors stay retryable.
const UNPROJECTABLE_EXIT: i32 = 3;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Rejected {
    leg: String,
    reason: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    leg: String,
    effort: Option<String>,
    u_core: Option<f64>,
    p_source: Option<String>,
    n: Option<u64>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Baselines {
    static_v0: Option<String>,
    strongest: Option<String>,
    cheapest: Option<String>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Route {
    eligible_n: usize,
    rejected: Vec<Rejected>,
    top5: Vec<Candidate>,
    chosen: Option<String>,
    explore: bool,
    pin: bool,
    baselines: Baselines,
}

fn route_from_json(value: Value) -> Result<Route, String> {
    let route: Route = serde_json::from_value(value).map_err(|error| error.to_string())?;
    let valid_name = |value: &str| !value.trim().is_empty() && value.len() <= 512;
    if route.top5.len() > route.eligible_n.min(5)
        || route.top5.iter().any(|row| !valid_name(&row.leg))
        || route
            .rejected
            .iter()
            .any(|row| !valid_name(&row.leg) || !valid_name(&row.reason))
        || route.chosen.as_deref().is_some_and(|id| !valid_name(id))
    {
        return Err("invalid attempt route entries".into());
    }
    Ok(route)
}

fn route(plan: &Value, chosen: &Value, native: bool) -> Result<Route, String> {
    let candidates: BTreeMap<_, _> = plan["candidates"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| Some((row["leg"].as_str()?, row)))
        .collect();
    let legs: Vec<_> = plan["legs"].as_array().into_iter().flatten().collect();
    let rejected: Vec<_> = plan["skipped"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| row["leg"].is_string() && row["reason"].is_string())
        .map(|row| json!({"leg":row["leg"],"reason":row["reason"]}))
        .collect();
    let mut scores: Vec<_> = legs
        .iter()
        .filter_map(|leg| {
            let id = leg["id"].as_str()?;
            let row = candidates.get(id).copied().unwrap_or(&Value::Null);
            Some(json!({
                "leg":id, "effort":leg["effort"], "u_core":row["u_core"],
                "p_source":row["p_source"], "n":row["n"]
            }))
        })
        .collect();
    scores.sort_by(|a, b| {
        let value = |row: &Value| row["u_core"].as_f64().unwrap_or(f64::NEG_INFINITY);
        value(b).total_cmp(&value(a))
    });
    scores.truncate(5);
    let baseline = |metric: &str, descending| {
        legs.iter()
            .filter_map(|leg| {
                let id = leg["id"].as_str()?;
                let value = candidates.get(id)?[metric].as_f64()?;
                Some((if descending { -value } else { value }, id))
            })
            .min_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(b.1)))
            .map(|(_, id)| id)
    };
    route_from_json(json!({
        "eligible_n": legs.len(), "rejected": rejected, "top5": scores, "chosen": chosen,
        "explore": plan["explore"].as_bool().unwrap_or(false),
        "pin": native || plan["p_source"] == "override",
        "baselines": {"static_v0":plan["static_v0"], "strongest":baseline("intelligence",true),
                      "cheapest":baseline("cost_per_task",false)}
    }))
}

pub(crate) fn terminal(run: &Value) -> bool {
    run["ended"].is_number()
        && !matches!(
            run["status"].as_str(),
            None | Some("running" | "queued" | "queue_claimed" | "dry_run")
        )
}

pub(crate) fn project(run: Value) -> Result<Value, String> {
    if !terminal(&run) {
        return Ok(Value::Null);
    }
    let last = run["attempts"].as_array().and_then(|rows| {
        rows.iter().rev().find(|row| {
            row["leg"].is_object()
                && !matches!(
                    row["status"].as_str(),
                    None | Some("skipped" | "dry_run" | "running")
                )
                && row["served_witness_reason"] != "leg refused before launch"
        })
    });
    let native = run["run_kind"] == "native-codex";
    let harness = run["harness"]
        .as_str()
        .unwrap_or(if native { "codex" } else { "unwitnessed" });
    let fallback = json!({
        "id": if native { "native-codex" } else { run["leg"].as_str().unwrap_or(harness) },
        "harness": harness, "model": run["requested_model"], "effort": run["effort"]
    });
    let (leg, witness) = if let Some(last) = last {
        (&last["leg"], last)
    } else if run["chosen_leg"].is_object() {
        (&run["chosen_leg"], &run)
    } else {
        (&fallback, &run)
    };
    for name in ["run_id", "host", "role"] {
        if !run[name].as_str().is_some_and(|value| !value.is_empty()) {
            return Err(format!("spawn record missing {name}"));
        }
    }
    let plan = &run["plan"];
    let effort = &plan["effort"];
    let note = witness["skip_reason"]
        .as_str()
        .or_else(|| run["reason"].as_str())
        .unwrap_or("");
    let exit_class = match (run["status"].as_str(), note.split(':').next().unwrap_or("")) {
        (Some("completed" | "succeeded"), _) if run["exit_code"] == 0 => "ok",
        (Some("cancelled" | "killed"), _) => "cancelled",
        (Some("timeout" | "timed_out"), _) => "timeout",
        (Some("stalled" | "stalled_no_leg"), _) => "stall",
        (_, "usage_limit" | "credit_limit" | "quota_limited") => "usage_limit",
        (_, "outage" | "not_served_on_host") => "outage",
        (_, "check_fail") => "check_fail",
        (_, "writer_no_changes") => "no_change",
        (Some("refused"), _) => "contract_defect",
        _ => "worker_crash",
    };
    Ok(json!({
        "attempt_id":run["run_id"], "role":run["role"], "host":run["host"],
        "card_key":run["item_key"].as_str().filter(|key| crate::records::valid_item(key)),
        "harness":leg["harness"].as_str().unwrap_or(harness),
        "leg":leg["id"].as_str().or_else(|| fallback["id"].as_str()),
        "requested_model":leg["model"].as_str().or_else(|| run["requested_model"].as_str()),
        "served_model":witness["served_model"].as_str().unwrap_or("unwitnessed"), "effort":leg["effort"],
        "level":effort["level"], "level_reason":effort["reason"],
        "bucket":effort["bucket"],
        "task_class":effort["task_class"].as_str().or_else(|| run["task_class"].as_str()).unwrap_or("adhoc"),
        "risk_band":effort["risk_band"],
        "plan_hash":crate::pyjson::card_hash(plan), "route":route(plan, &leg["id"], native)?,
        "started":run["started"], "ended":run["ended"], "exit_class":exit_class,
        "tokens_in":witness["usage"]["input_tokens"], "tokens_out":witness["usage"]["output_tokens"],
        "cost":witness["usage"]["cost"]
    }))
}

pub(crate) fn execute(verb: &str, args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if !args.is_empty() {
        return Err(format!("{verb} takes JSON on stdin"));
    }
    let input = serde_json::from_reader(std::io::stdin().lock()).map_err(|e| e.to_string())?;
    let value = match verb {
        "project-spawn-attempt" => project(input).unwrap_or_else(|error| {
            eprintln!("{error}");
            std::process::exit(UNPROJECTABLE_EXIT);
        }),
        "validate-attempt-route" => json!(route_from_json(input)?),
        _ => return Err("unknown attempt command".into()),
    };
    writeln!(out, "{value}").map_err(|e| e.to_string())
}
