//! A review quota refusal consumes an attempt, never a second run or a reset wait.
use crate::{launch::LaunchAdapter, placement::Args, runs};
use pg_worker_core::run::{
    record::{new_job_id, RunRecord, Status},
    supervise::{FinishHook, RunSpec},
};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

pub(crate) fn hook(args: Args, leg: Value, attempt: u32) -> FinishHook {
    Box::new(move |record, status| {
        let quota = if record.get("role") == Some(&json!("review")) {
            pg_worker_core::run::review_quota::reason(
                record
                    .get("stdout_tail")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                record
                    .get("stderr_tail")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
        } else {
            None
        };
        if let Some(reason) = quota
            .as_ref()
            .filter(|_| status != Status::Cancelled && record.get("survivors").is_none())
        {
            record.set("reason", json!(reason));
        }
        runs::finish(record, status, &leg, attempt);
        if quota.is_none()
            || status == Status::Cancelled
            || record.get("survivors").is_some()
            || args.get("no_fallthrough") == Some(&json!(true))
            || status == Status::DryRun
        {
            return None;
        }
        match next(record, args, &leg, attempt) {
            Ok(next) => next,
            Err(error) => {
                record.set("error", json!(error.into_error_data().message));
                None
            }
        }
    })
}
fn next(
    record: &mut RunRecord,
    args: Args,
    previous: &Value,
    attempt: u32,
) -> Result<Option<RunSpec>, crate::errors::ToolError> {
    let value = record.to_value();
    let legs = value["plan"]["legs"].as_array();
    let Some(index) = legs.and_then(|legs| legs.iter().position(|leg| leg["id"] == previous["id"]))
    else {
        return Ok(None);
    };
    let writers = args.get("writer_families").and_then(Value::as_array);
    let next = legs.unwrap()[index + 1..].iter().find(|leg| {
        !writers.is_some_and(|writers| writers.contains(&leg["family"]))
            && !value["attempts"]
                .as_array()
                .is_some_and(|attempts| attempts.iter().any(|a| a["leg"]["id"] == leg["id"]))
    });
    let Some(leg) = next.cloned() else {
        return Ok(None);
    };
    let state = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap();
    let run_id = value["run_id"].as_str().unwrap();
    let job_id = new_job_id();
    let mut launch = LaunchAdapter::from_env().request(state, &json!({
        "run_id":run_id,"job_id":job_id,"role":"review","dry_run":false,"leg":leg,
        "prompt":args["prompt"],"card":args["card"],"cwd":value["cwd"],"state":state.to_string_lossy()
    }), Duration::from_secs(30))?;
    let mut review_args = json!(args);
    review_args["role"] = json!("review");
    crate::prompt::prepare_review(
        &review_args,
        &leg,
        &mut launch,
        &crate::prompt::repos(),
        crate::prompt::configured_mode("review") == "on",
    )
    .map_err(|e| crate::errors::ToolError::Internal(e.to_string()))?;
    let mut metadata = json!({});
    runs::store_prompt(state, run_id, &mut launch, &mut metadata, false)?;
    for (key, val) in metadata.as_object().unwrap() {
        record.set(key, val.clone());
    }
    let mut attempts = value["attempts"].as_array().unwrap().clone();
    attempts.push(json!({"leg":leg,"status":"running"}));
    record.set("attempts", json!(attempts));
    for key in [
        "reason",
        "error",
        "exit_code",
        "request_side_model",
        "witness_kind",
    ] {
        record.set(key, Value::Null);
    }
    for key in ["served_model", "served_family"] {
        record.set(key, json!("unwitnessed"));
    }
    record.set(
        "served_witness_reason",
        json!("no authenticated harness telemetry; skipped or unobserved leg"),
    );
    for key in ["stdout_tail", "stderr_tail"] {
        record.set(key, json!(""));
    }
    for event in launch.events.unwrap_or_default() {
        let _ = record.push_event(Value::Object(event));
    }
    let _ = record.push_event(
        json!({"event":"review_quota_fallthrough","from":previous["id"],"to":leg["id"]}),
    );
    record.set("exit_code", Value::Null);
    record.save().map_err(|_| {
        crate::errors::ToolError::Internal("could not save review fallthrough".into())
    })?;
    Ok(Some(RunSpec {
        run_id: run_id.into(),
        job_id,
        program: launch.argv[0].clone().into(),
        args: launch.argv[1..].iter().map(Into::into).collect(),
        cwd: launch.cwd,
        env: launch
            .env
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect(),
        stdin: launch.stdin.map(String::into_bytes),
        base: Default::default(),
        events: vec![],
        finish: Some(hook(args, leg, attempt + 1)),
    }))
}

pub(crate) fn witness(record: &mut RunRecord, leg: &Value, attempt: u32) {
    // Claude's init metadata is response-side evidence. Never infer a witness
    // from the requested model, review prose, or child-writable database.
    if leg["harness"] != "claude" {
        return;
    }
    let models: std::collections::BTreeSet<String> = record
        .get("stdout_tail")
        .and_then(Value::as_str)
        .unwrap_or("")
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|event| event["type"] == "system" && event["subtype"] == "init")
        .filter_map(|event| {
            event["model"]
                .as_str()
                .filter(|model| !model.trim().is_empty())
                .map(str::to_owned)
        })
        .collect();
    if models.len() != 1 {
        return;
    }
    let model = models.into_iter().next().unwrap();
    record.set("served_model", json!(model));
    record.set("served_family", json!("anthropic"));
    record.set("witness_kind", json!("native_harness"));
    record.set("request_side_model", leg["model"].clone());
    record.set("served_witness_reason", json!("claude system init model"));
    let mut attempts = record.get("attempts").cloned().unwrap_or(json!([]));
    if let Some(row) = attempts
        .as_array_mut()
        .and_then(|a| a.get_mut(attempt as usize - 1))
    {
        for key in [
            "served_model",
            "served_family",
            "witness_kind",
            "request_side_model",
            "served_witness_reason",
        ] {
            row[key] = record.get(key).cloned().unwrap_or(Value::Null);
        }
        record.set("attempts", attempts);
    }
}
