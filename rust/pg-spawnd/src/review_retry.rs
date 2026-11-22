//! A review quota refusal consumes an attempt, never a second run or a reset wait.
use crate::{launch::LaunchAdapter, placement::Args, runs};
use pg_worker_core::run::{
    record::{new_job_id, RunRecord, Status},
    supervise::{FinishHook, RunSpec},
};
use serde_json::{json, Value};
use std::{collections::BTreeSet, path::Path, time::Duration};

pub(crate) fn hook_with_group(
    args: Args,
    leg: Value,
    attempt: u32,
    checkout: Option<tempfile::TempDir>,
    allow_shas: BTreeSet<String>,
    group: Option<(usize, usize, Vec<String>)>,
) -> FinishHook {
    Box::new(move |record, status| {
        let _checkout = checkout;
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
        runs::finish_with_allowlist(record, status, &leg, attempt, &allow_shas);
        if quota.is_none()
            || status == Status::Cancelled
            || record.path().with_file_name("cancelled").exists()
            || record.get("survivors").is_some()
            || args.get("no_fallthrough") == Some(&json!(true))
            || status == Status::DryRun
        {
            return None;
        }
        match next(record, args, &leg, attempt, group) {
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
    group: Option<(usize, usize, Vec<String>)>,
) -> Result<Option<RunSpec>, crate::errors::ToolError> {
    let value = record.to_value();
    let legs = value["plan"]["legs"].as_array();
    let Some(index) = legs.and_then(|legs| legs.iter().position(|leg| leg["id"] == previous["id"]))
    else {
        return Ok(None);
    };
    for leg in &legs.unwrap()[index + 1..] {
        // The routed plan and launch admission share the Python writer resolver.
        if record
            .get("attempts")
            .and_then(Value::as_array)
            .is_some_and(|attempts| attempts.iter().any(|a| a["leg"]["id"] == leg["id"]))
        {
            continue;
        }
        if record.path().with_file_name("cancelled").exists() {
            return Ok(None);
        }
        match prepare(
            record,
            &args,
            leg,
            group.as_ref().map(|(i, t, p)| (*i, *t, p.as_slice())),
        ) {
            Ok(_) if record.path().with_file_name("cancelled").exists() => return Ok(None),
            Ok(prepared) => {
                return advance(
                    record,
                    args,
                    previous,
                    attempt,
                    leg.clone(),
                    prepared,
                    group,
                )
                .map(Some)
            }
            Err(error) => {
                let mut attempts = record.get("attempts").cloned().unwrap_or(json!([]));
                attempts
                    .as_array_mut()
                    .unwrap()
                    .push(json!({"leg":leg,"status":"skipped",
                    "skip_reason":error.into_error_data().message}));
                record.set("attempts", attempts);
            }
        }
    }
    Ok(None)
}
type Prepared = (String, crate::launch::LaunchSpec, Option<String>);
fn prepare(
    record: &RunRecord,
    args: &Args,
    leg: &Value,
    group: Option<(usize, usize, &[String])>,
) -> Result<Prepared, crate::errors::ToolError> {
    let value = record.to_value();
    let state = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap();
    let run_id = value["run_id"].as_str().unwrap();
    let job_id = new_job_id();
    let cwd = if crate::cli_review::eligible(leg) {
        crate::cli_review::cwd(args, value["cwd"].as_str().unwrap().into())?
    } else {
        value["cwd"].as_str().unwrap().into()
    };
    let mut launch = LaunchAdapter::from_env().request(
        state,
        &json!({
            "run_id":run_id,"job_id":job_id,"role":"review","dry_run":false,"leg":leg,
            "writer_families":args.get("writer_families").cloned().unwrap_or(json!([])),
            "review_of":args.get("review_of"),
            "prompt":args["prompt"],"card":args["card"],"cwd":cwd,"state":state.to_string_lossy()
        }),
        Duration::from_secs(30),
    )?;
    let head = crate::prompt::prepare_launch_group(args, leg, &mut launch, false, group)?;
    if head.is_some()
        && value["head_sha"]
            .as_str()
            .is_some_and(|old| head.as_deref() != Some(old))
    {
        return Err(crate::placement::parameter(
            "CLI review head changed between attempts",
        ));
    }
    Ok((job_id, launch, head))
}

fn advance(
    record: &mut RunRecord,
    args: Args,
    previous: &Value,
    attempt: u32,
    leg: Value,
    prepared: Prepared,
    group: Option<(usize, usize, Vec<String>)>,
) -> Result<RunSpec, crate::errors::ToolError> {
    let value = record.to_value();
    let state = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap();
    let run_id = value["run_id"].as_str().unwrap();
    let (job_id, mut launch, head) = prepared;
    let mut metadata = json!({"cwd":launch.cwd});
    if let Some(head) = head {
        metadata["head_sha"] = json!(head);
    }
    let pre_isolate_cwd = launch.cwd.clone();
    let checkout = crate::cli_review::isolate(&mut launch, &leg, metadata["head_sha"].as_str())?;
    metadata["review_checkout"] = if checkout.is_some() {
        json!(launch.cwd)
    } else {
        Value::Null
    };
    let allow_shas = runs::allow_shas(
        &args,
        &launch,
        &pre_isolate_cwd,
        metadata["head_sha"].as_str(),
    );
    runs::store_prompt(
        state,
        run_id,
        &mut launch,
        &mut metadata,
        false,
        &allow_shas,
    )?;
    let log = record.path().with_file_name("live.log");
    let archive = record
        .path()
        .with_file_name(format!("live-attempt-{attempt}.log"));
    if log.exists() {
        std::fs::rename(&log, &archive).map_err(|_| {
            crate::errors::ToolError::Internal("could not archive review attempt log".into())
        })?;
    }
    for (key, val) in metadata.as_object().unwrap() {
        record.set(key, val.clone());
    }
    let mut attempts = value["attempts"].as_array().unwrap().clone();
    attempts.push(runs::booked_attempt(&leg));
    let next_attempt = attempts.len() as u32;
    record.set("attempts", json!(attempts));
    for key in [
        "reason",
        "error",
        "exit_code",
        "request_side_model",
        "witness_kind",
        "verdict",
        "findings",
        "test_gaming",
        "finish_reason",
        "completion",
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
    Ok(RunSpec {
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
        finish: Some(hook_with_group(
            args,
            leg,
            next_attempt,
            checkout,
            allow_shas.clone(),
            group,
        )),
        allow_shas,
    })
}

pub(crate) fn witness(record: &mut RunRecord, leg: &Value, attempt: u32) {
    // Claude's init metadata is response-side evidence. Never infer a witness
    // from the requested model, review prose, or child-writable database.
    if leg["harness"] != "claude" || crate::cli_review::eligible(leg) {
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
