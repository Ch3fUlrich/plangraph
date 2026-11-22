//! Readable rows over existing spawner records. Each admitted leg has one stable identity.
use serde_json::{json, Value};
use std::{collections::BTreeMap, io::Write};

fn route(plan: &Value, chosen: &Value) -> Value {
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
    let mut top: Vec<_> = legs
        .iter()
        .filter_map(|leg| {
            let id = leg["id"].as_str()?;
            let candidate = candidates.get(id).copied().unwrap_or(&Value::Null);
            Some(json!({
                "leg":id, "effort":leg["effort"], "u_core":candidate["u_core"],
                "p_source":candidate["p_source"], "n":candidate["n"]
            }))
        })
        .collect();
    top.sort_by(|a, b| {
        let score = |row: &Value| row["u_core"].as_f64().unwrap_or(f64::NEG_INFINITY);
        score(b).total_cmp(&score(a))
    });
    top.truncate(5);
    let baseline = |metric: &str, descending: bool| {
        legs.iter()
            .filter_map(|leg| {
                let id = leg["id"].as_str()?;
                let score = candidates.get(id)?[metric].as_f64()?;
                Some((if descending { -score } else { score }, id))
            })
            .min_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(b.1)))
            .map(|(_, id)| id)
    };
    json!({
        "eligible_n": if plan["legs"].is_array() { Some(legs.len()) } else { None },
        "rejected": rejected, "top5": top, "chosen": chosen,
        "explore": plan["explore"].as_bool().unwrap_or(false),
        "pin": plan["p_source"] == "override",
        "baselines": {
            "static_v0": plan["static_v0"],
            "strongest":baseline("intelligence",true), "cheapest":baseline("cost_per_task",false)
        }
    })
}

fn row(run: &Value, attempt: &Value, index: usize, only: bool, since: f64) -> Option<(f64, Value)> {
    let status = attempt["status"].as_str()?;
    if matches!(status, "skipped" | "dry_run" | "queued" | "refused")
        || attempt["served_witness_reason"] == "leg refused before launch"
    {
        return None;
    }
    let started = attempt["started"]
        .as_f64()
        .or_else(|| run["started"].as_f64())?;
    if started < since {
        return None;
    }
    let run_id = run["run_id"].as_str()?;
    let leg = &attempt["leg"];
    let leg_id = leg.get("id").unwrap_or(leg);
    let plan = &run["plan"];
    let effort = &plan["effort"];
    let ended = attempt["ended"]
        .as_f64()
        .or_else(|| only.then(|| run["ended"].as_f64()).flatten());
    let wall = ended.filter(|end| *end >= started).map(|end| end - started);
    let card_key = run["item_key"]
        .as_str()
        .filter(|id| crate::records::valid_item(id))
        .unwrap_or("adhoc");
    let served_model = attempt["served_model"]
        .as_str()
        .filter(|model| !model.is_empty())
        .unwrap_or("unwitnessed");
    let host = run["host"].as_str().filter(|v| !v.is_empty());
    Some((
        started,
        json!({
            "attempt_id": format!("{run_id}:{}", index + 1), "run_id":run_id,
            "card_key":card_key, "role":run["role"], "host":host,
            "harness":leg["harness"], "leg":leg_id, "requested_model":leg["model"],
            "served_model":served_model, "effort":leg["effort"],
            "level":effort["level"], "level_reason":effort["reason"],
            "task_class":effort["task_class"], "risk_band":effort["risk_band"], "bucket":effort["bucket"],
            "plan_hash":plan.is_object().then(|| crate::pyjson::card_hash(plan)),
            "route":route(plan, leg_id), "status":status,
            "started":started, "ended":ended, "wall_seconds":wall,
            "tokens_in":attempt["usage"]["input_tokens"], "tokens_out":attempt["usage"]["output_tokens"]
        }),
    ))
}

pub(crate) fn rows(records: &[Value], since: f64) -> Vec<Value> {
    let mut rows = Vec::new();
    for run in records {
        if matches!(
            run["status"].as_str(),
            Some("dry_run" | "queued" | "refused")
        ) {
            continue;
        }
        let attempts = run["attempts"]
            .as_array()
            .filter(|attempts| !attempts.is_empty());
        if let Some(attempts) = attempts {
            let only = attempts
                .iter()
                .filter(|entry| entry["status"] != "skipped")
                .count()
                == 1;
            rows.extend(
                attempts
                    .iter()
                    .enumerate()
                    .filter_map(|(index, attempt)| row(run, attempt, index, only, since)),
            );
        } else if run["pid"].is_u64() || run["job_id"].is_string() {
            let native = run["run_kind"] == "native-codex";
            let leg = if native {
                json!({"id":"native-codex","harness":"codex",
                    "model":run["requested_model"],"effort":run["effort"]})
            } else {
                run["chosen_leg"].clone()
            };
            if leg.is_object() {
                let attempt = json!({"leg":leg,"status":run["status"],
                    "served_model":run["served_model"],"usage":run["usage"]});
                rows.extend(row(run, &attempt, 0, true, since));
            }
        }
    }
    rows.sort_by(|a, b| {
        a.0.total_cmp(&b.0)
            .then_with(|| a.1["attempt_id"].as_str().cmp(&b.1["attempt_id"].as_str()))
    });
    rows.into_iter().map(|(_, row)| row).collect()
}

fn window(value: &str) -> Option<u64> {
    let (number, multiplier) = match value.as_bytes().last()? {
        b'd' => (value.strip_suffix('d')?, 86_400),
        b'h' => (value.strip_suffix('h')?, 3_600),
        _ => return None,
    };
    number
        .parse::<u64>()
        .ok()?
        .checked_mul(multiplier)
        .filter(|n| *n > 0)
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    let mut args = args.into_iter();
    let (mut state, mut seconds, mut json) = (None, None, false);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--state" if state.is_none() => {
                state = Some(args.next().ok_or("missing --state value")?);
            }
            "--since" if seconds.is_none() => {
                seconds = Some(
                    args.next()
                        .as_deref()
                        .and_then(window)
                        .ok_or("use a positive window such as 1d or 12h")?,
                );
            }
            "--json" if !json => json = true,
            _ => return Err("usage: attempts --since 1d --json [--state PATH]".into()),
        }
    }
    let since = crate::effort_level::epoch_secs().saturating_sub(seconds.ok_or("missing --since")?);
    let state = crate::state::resolve_state(state.as_deref(), |key| std::env::var_os(key))?;
    let records = crate::records::list_runs(&state);
    let result = rows(&records, since as f64);
    if json {
        serde_json::to_writer(&mut *out, &result).map_err(|e| e.to_string())?;
        writeln!(out).map_err(|e| e.to_string())
    } else {
        for row in result {
            writeln!(out, "{}", row).map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}
