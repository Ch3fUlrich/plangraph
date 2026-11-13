//! Progress snapshots and server-owned waiting for exact package sessions.
use crate::{
    errors::ToolError,
    package::{self, Backend, Host},
    placement::{parameter, Args},
};
use serde_json::{json, Value};
use std::{
    path::Path,
    time::{Duration, Instant},
};
fn exit_code(text: &str) -> Option<i64> {
    let re = regex::Regex::new(r"(?m)^EXIT (-?[0-9]+)\r?$").unwrap();
    re.captures_iter(text)
        .last()
        .and_then(|c| c[1].parse().ok())
}
fn classify(run: &Value, sample: &Value, now: f64, stall_seconds: f64) -> Value {
    let previous = &run["snapshot"];
    let changed = ["log_size", "log_mtime", "status_text", "commits"]
        .iter()
        .any(|k| sample[*k] != previous[*k]);
    let progress = if changed {
        now
    } else {
        run["last_progress"]
            .as_f64()
            .or_else(|| run["started"].as_f64())
            .unwrap_or(now)
    };
    let idle = (now - progress).max(0.0);
    let text = sample["status_text"].as_str().unwrap_or("");
    let all_done = text.lines().any(|line| line.trim() == "ALL DONE");
    let code = exit_code(sample["log_tail"].as_str().unwrap_or(""));
    let alive = sample["alive"] == true;
    let agent = sample["agent_status"].as_str().unwrap_or("");
    let finished = all_done || code == Some(0);
    let verdict = if finished && (!alive || ["idle", "done"].contains(&agent)) {
        "idle-finished"
    } else if !alive {
        "dead"
    } else if agent == "blocked" {
        "blocked-prompt"
    } else if idle >= stall_seconds {
        "stalled"
    } else {
        "running"
    };
    let mut counts = json!({});
    for name in ["DONE", "SKIPPED", "REFUTED"] {
        counts[name] = json!(text
            .lines()
            .filter(|l| l.trim() != "ALL DONE" && l.split_whitespace().any(|w| w == name))
            .count());
    }
    let delta =
        |name: &str| sample[name].as_f64().unwrap_or(0.0) - previous[name].as_f64().unwrap_or(0.0);
    json!({"verdict":verdict,"alive":alive,"agent_status":agent,"all_done":all_done,"exit_code":code,
        "counts":counts,"last_line":text.lines().last().unwrap_or(""),"log_delta":delta("log_size"),
        "mtime_delta":delta("log_mtime"),"commits":sample["commits"],"idle_seconds":idle,"last_progress":progress,
        "snapshot":sample,"probed_at":now,"load":sample["load"],"ram_available_mb":sample["ram_available_mb"],"disk_free_mb":sample["disk_free_mb"]})
}
fn unhex(value: &Value) -> Result<String, ToolError> {
    let s = value
        .as_str()
        .ok_or_else(|| parameter("invalid probe hex field"))?;
    if s.len() > 1024 * 1024 || s.len() % 2 != 0 {
        return Err(parameter("invalid probe hex size"));
    }
    let bytes = (0..s.len())
        .step_by(2)
        .map(|i| s.get(i..i + 2).and_then(|s| u8::from_str_radix(s, 16).ok()))
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| parameter("invalid probe hex"))?;
    String::from_utf8(bytes).map_err(|_| parameter("probe text is not UTF-8"))
}
fn normalize(run: &Value, mut sample: Value) -> Result<Value, ToolError> {
    if sample.get("agents_hex").is_some() {
        let agents: Value = serde_json::from_str(&unhex(&sample["agents_hex"])?)
            .map_err(|_| parameter("invalid herdr agent list"))?;
        let agents = agents
            .pointer("/result/agents")
            .and_then(Value::as_array)
            .ok_or_else(|| parameter("invalid herdr agents"))?;
        let exact = agents
            .iter()
            .find(|a| a["name"] == run["agent_name"] && a["pane_id"] == run["pane"]);
        sample["alive"] = json!(exact.is_some());
        sample["agent_status"] = exact
            .map(|a| a["agent_status"].clone())
            .unwrap_or(json!("dead"));
        sample["log_tail"] = json!(unhex(&sample["log_hex"])?);
        sample["status_text"] = json!(unhex(&sample["status_hex"])?);
        for key in ["agents_hex", "log_hex", "status_hex"] {
            sample.as_object_mut().unwrap().remove(key);
        }
    }
    if !sample["alive"].is_boolean()
        || !sample["log_size"].is_number()
        || !sample["commits"].is_number()
        || !sample["status_text"].is_string()
    {
        return Err(parameter("incomplete package probe"));
    }
    Ok(sample)
}
pub(crate) fn one(
    state: &Path,
    mut run: Value,
    host: &Host,
    backend: &mut impl Backend,
    stall: f64,
) -> Result<Value, ToolError> {
    let sample = backend.remote(host, &crate::package_probe_scripts::script(host, &run))?;
    let sample = normalize(&run, sample)?;
    let result = classify(&run, &sample, package::now(), stall);
    run.as_object_mut()
        .unwrap()
        .extend(result.as_object().unwrap().clone());
    run["status"] = json!(match run["verdict"].as_str() {
        Some("idle-finished") => "completed",
        Some("dead") => "failed",
        Some("blocked-prompt") => "blocked",
        _ => "running",
    });
    package::save(state, &run)?;
    Ok(run)
}
fn stall(args: &Args) -> Result<f64, ToolError> {
    let n = args
        .get("stalled_min")
        .map_or(Some(10.0), Value::as_f64)
        .filter(|n| n.is_finite() && *n > 0.0 && *n <= 1440.0)
        .ok_or_else(|| parameter("stalled_min must be in (0, 1440]"))?;
    Ok(n * 60.0)
}
pub(crate) fn probe(state: &Path, args: &Args) -> Result<Value, ToolError> {
    probe_deadline(state, args, None)
}
fn probe_deadline(
    state: &Path,
    args: &Args,
    deadline: Option<Instant>,
) -> Result<Value, ToolError> {
    let name = args.get("run").and_then(Value::as_str).unwrap_or("all");
    let runs = if name == "all" {
        package::list(state)
    } else {
        vec![package::read(state, name)?]
    };
    let threshold = stall(args)?;
    let mut result = Vec::new();
    let mut backend = crate::package_transport::Native { deadline };
    for run in runs {
        let host = package::configured_host(state, run["host"].as_str().unwrap_or(""))?;
        result.push(package::view(one(
            state,
            run,
            &host,
            &mut backend,
            threshold,
        )?));
    }
    Ok(json!({"runs":result}))
}
fn ready(result: &Value, until: &str) -> bool {
    result["runs"].as_array().is_some_and(|runs| {
        runs.iter().any(|r| match until {
            "any_done" => matches!(r["verdict"].as_str(), Some("idle-finished" | "dead")),
            _ => matches!(
                r["verdict"].as_str(),
                Some("stalled" | "dead" | "blocked-prompt")
            ),
        })
    })
}
pub(crate) fn wait(state: &Path, args: &Args) -> Result<Value, ToolError> {
    let until = args
        .get("until")
        .and_then(Value::as_str)
        .unwrap_or("any_done");
    if !["any_done", "stalled"].contains(&until) {
        return Err(parameter("until must be any_done or stalled"));
    }
    let timeout = args
        .get("timeout")
        .map_or(Some(300.0), Value::as_f64)
        .filter(|n| n.is_finite() && *n >= 0.0 && *n <= 3600.0)
        .ok_or_else(|| parameter("timeout must be in [0, 3600] seconds"))?;
    let runs = args
        .get("runs")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty() && v.len() <= 100 && v.iter().all(Value::is_string))
        .ok_or_else(|| parameter("runs must contain 1-100 run ids"))?;
    // Validate all identifiers before the first remote action.
    for run in runs {
        package::read(state, run.as_str().unwrap())?;
    }
    let started = Instant::now();
    let deadline = started + Duration::from_secs_f64(timeout);
    loop {
        if Instant::now() >= deadline {
            let rows = runs
                .iter()
                .filter_map(|id| package::read(state, id.as_str().unwrap()).ok())
                .map(package::view)
                .collect::<Vec<_>>();
            return Ok(json!({"runs":rows,"timed_out":true}));
        }
        let mut rows = Vec::new();
        for run in runs {
            let mut params = args.clone();
            params.insert("run".into(), run.clone());
            rows.extend(
                probe_deadline(state, &params, Some(deadline))?["runs"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .cloned(),
            );
        }
        let mut result = json!({"runs":rows,"timed_out":false});
        if ready(&result, until) {
            return Ok(result);
        }
        let remaining = Duration::from_secs_f64(timeout).saturating_sub(started.elapsed());
        if remaining.is_zero() {
            result["timed_out"] = json!(true);
            return Ok(result);
        }
        std::thread::sleep(remaining.min(Duration::from_secs(2)));
    }
}
#[cfg(test)]
#[path = "package_probe_tests.rs"]
mod tests;
