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
        let weak = quota.is_none()
            && status == Status::Completed
            && record.get("role").and_then(Value::as_str) == Some("review")
            && !crate::cli_review::binding_completed(record)
            && weak_acceptance(record);
        let status = if weak {
            record.set("verdict", json!("no_verdict"));
            record.set("review_status", json!("no_verdict"));
            record.set("reason", json!("no_verdict: files_read_missing"));
            record.set("exit_code", json!(6));
            Status::Failed
        } else {
            status
        };
        runs::finish_with_allowlist(
            record,
            status,
            &leg,
            attempt,
            &allow_shas,
            args.get("card").unwrap_or(&Value::Null),
        );
        let may_retry = (quota.is_some() || weak)
            && !crate::cli_review::binding_completed(record)
            && status != Status::Cancelled
            && record.status().is_some_and(|status| !status.is_terminal())
            && !record.path().with_file_name("cancelled").exists()
            && record.get("survivors").is_none()
            && args.get("no_fallthrough") != Some(&json!(true))
            && status != Status::DryRun;
        if may_retry {
            match next(record, args, &leg, attempt, group) {
                Ok(Some(next)) => return Some(next),
                Ok(None) => {}
                Err(error) => record.set("error", json!(error.into_error_data().message)),
            }
        }
        if weak && !record.path().with_file_name("cancelled").exists() {
            let _ = record.transition(Status::Failed, pg_worker_core::run::finalize::now());
        }
        None
    })
}
fn weak_acceptance(record: &RunRecord) -> bool {
    use pg_worker_core::review_split::{has_files_read, parse_verdict_text, SeatVerdict};
    let Some(state) = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
    else {
        return false;
    };
    crate::review_artifacts::review_text(state, &record.to_value()).is_ok_and(|text| {
        parse_verdict_text(&text) == Some(SeatVerdict::Pass) && !has_files_read(&text)
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
        // A later leg with a seat bound takes a slot like any other seat; a full leg is skipped.
        let slot = match crate::seat_admission::reserve_now(
            state_of(record),
            leg,
            &value["plan"]["seat_admission"],
        ) {
            Ok(slot) => slot,
            Err(error) => {
                skip(record, leg, error.into_error_data().message);
                continue;
            }
        };
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
                    slot,
                )
                .map(Some)
            }
            Err(error) => skip(record, leg, error.into_error_data().message),
        }
    }
    Ok(None)
}
fn skip(record: &mut RunRecord, leg: &Value, reason: impl Into<String>) {
    let mut attempts = record.get("attempts").cloned().unwrap_or(json!([]));
    attempts
        .as_array_mut()
        .unwrap()
        .push(json!({"leg":leg,"status":"skipped","skip_reason":reason.into()}));
    record.set("attempts", attempts);
}
fn state_of(record: &RunRecord) -> &Path {
    record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap()
}
type Prepared = (
    String,
    crate::launch::LaunchSpec,
    Option<crate::cli_review::Dispatch>,
);
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
    let review = crate::prompt::prepare_launch_group(args, leg, &mut launch, false, group)?;
    if let Some(dispatch) = &review {
        if value["head_sha"]
            .as_str()
            .is_some_and(|old| dispatch.head != old)
            || (!value["review_binding"].is_null()
                && dispatch.binding.as_ref() != Some(&value["review_binding"]))
        {
            return Err(crate::placement::parameter(
                "CLI review diff changed between attempts",
            ));
        }
    }
    Ok((job_id, launch, review))
}

#[allow(clippy::too_many_arguments)]
fn advance(
    record: &mut RunRecord,
    args: Args,
    previous: &Value,
    attempt: u32,
    leg: Value,
    prepared: Prepared,
    group: Option<(usize, usize, Vec<String>)>,
    slot: Option<crate::seat_admission::Reservation>,
) -> Result<RunSpec, crate::errors::ToolError> {
    let value = record.to_value();
    let state = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .unwrap();
    let run_id = value["run_id"].as_str().unwrap();
    let (job_id, mut launch, review) = prepared;
    let mut metadata = json!({"cwd":launch.cwd});
    if let Some(review) = review {
        review.apply(&mut metadata);
    } else {
        metadata["review_binding"] = Value::Null;
        metadata["review_base"] = Value::Null;
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
    // The attempt's slot rides on the record, so the leg it now runs on counts it once.
    if let Some(slot) = &slot {
        record.set(crate::seat_admission::TOKEN_FIELD, json!(slot.token()));
    }
    let mut attempts = value["attempts"].as_array().unwrap().clone();
    attempts.push(json!({"leg":leg,"status":"running"}));
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
        "review_binding_error",
        "review_status",
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
    // The record names the slot now; a failed save above frees it instead (the guard drops).
    if let Some(slot) = slot {
        slot.keep();
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use pg_worker_core::run::record::new_run_id;

    #[test]
    fn non_cli_fallthrough_clears_previous_diff_binding() {
        let state = tempfile::tempdir().unwrap();
        let run_id = new_run_id();
        let base = "a".repeat(40);
        let head = "b".repeat(40);
        let previous = json!({"id":"agy-gemini-4-hydrogen-high","harness":"agy","model":"gemini-4-hydrogen-high"});
        let leg = json!({"id":"fixture-fallback","harness":"opencode"});
        let mut record = runs::create(
            state.path(),
            &run_id,
            "fixture",
            json!({
                "role":"review",
                "cwd":state.path(),
                "head_sha":head,
                "review_base":base,
                "review_binding":{"base_sha":base,"head_sha":head,"diff_sha256":"c".repeat(64)},
                "attempts":[{"leg":previous,"status":"failed","skip_reason":"quota"}],
            }),
            Status::Failed,
        )
        .ok()
        .unwrap();
        let args = json!({"role":"review","card":{}});
        let args = args.as_object().unwrap().clone();
        let mut launch = crate::launch::LaunchSpec {
            max_request_tokens: None,
            split_inline_diff: false,
            argv: vec!["opencode".into()],
            cwd: state.path().into(),
            env: Default::default(),
            stdin: Some("review".into()),
            prompt: Some("review".into()),
            events: None,
            warnings: None,
        };
        let review = crate::prompt::prepare_launch(&args, &leg, &mut launch, false)
            .ok()
            .unwrap();
        assert!(review.is_none());
        assert!(advance(
            &mut record,
            args,
            &previous,
            1,
            leg.clone(),
            (new_job_id(), launch, review),
            None,
            None,
        )
        .is_ok());
        let saved = crate::records::read_record(state.path(), &json!(run_id))
            .ok()
            .unwrap();
        assert_eq!(saved["attempts"][1]["leg"], leg);
        assert_eq!(saved["head_sha"], head);
        assert!(saved["review_binding"].is_null());
        assert!(saved["review_base"].is_null());
    }

    /// A retry into a leg that has no free seat skips that leg and starts nothing there.
    #[test]
    fn a_retry_into_a_saturated_leg_is_skipped_and_a_free_leg_keeps_its_slot_on_the_record() {
        let state = tempfile::tempdir().unwrap();
        let hydrogen = json!({"id":"agy-gemini-4-hydrogen-high","harness":"agy","model":"gemini-4-hydrogen-high"});
        let weak = json!({"id":"opencode-go-deepseek-v4-flash","harness":"opencode"});
        let policy = json!({"legs":{"agy-gemini-4-hydrogen-high":1},"queue_wait_seconds":900.0});
        // Another seat holds the one hydrogen slot.
        let held = crate::seat_admission::reserve_now(state.path(), &hydrogen, &policy)
            .ok()
            .unwrap()
            .unwrap();
        let run_id = new_run_id();
        let mut record = runs::create(
            state.path(),
            &run_id,
            "fixture",
            json!({
                "role":"review","cwd":state.path(),
                "plan":{"first_leg":weak,"legs":[weak,hydrogen],"seat_admission":policy},
                "attempts":[{"leg":weak,"status":"failed","skip_reason":"quota"}],
            }),
            Status::Failed,
        )
        .ok()
        .unwrap();
        let args = json!({"role":"review","card":{}})
            .as_object()
            .unwrap()
            .clone();
        assert!(next(&mut record, args, &weak, 1, None)
            .ok()
            .unwrap()
            .is_none());
        let saved = record.to_value();
        let skipped = &saved["attempts"][1];
        assert_eq!(skipped["leg"]["id"], "agy-gemini-4-hydrogen-high");
        assert_eq!(skipped["status"], "skipped");
        assert!(skipped["skip_reason"]
            .as_str()
            .unwrap()
            .starts_with("seat_queue_timeout:"));
        // The occupant still holds its slot: the refused retry took none.
        drop(held);
        assert!(
            crate::seat_admission::reserve_now(state.path(), &hydrogen, &policy)
                .ok()
                .unwrap()
                .is_some()
        );
    }

    /// Runs `advance` into a leg bounded to one seat, holding that seat; `break_save` makes the save fail.
    fn advance_with_slot(break_save: bool) -> (tempfile::TempDir, Value, bool) {
        let state = tempfile::tempdir().unwrap();
        let run_id = new_run_id();
        let previous = json!({"id":"first","harness":"opencode"});
        let leg = json!({"id":"fixture-fallback","harness":"opencode"});
        let policy = json!({"legs":{"fixture-fallback":1},"queue_wait_seconds":0.0});
        let mut record = runs::create(
            state.path(),
            &run_id,
            "fixture",
            json!({"role":"review","cwd":state.path(),
                "plan":{"first_leg":previous,"legs":[previous,leg],"seat_admission":policy},
                "attempts":[{"leg":previous,"status":"failed","skip_reason":"quota"}]}),
            Status::Failed,
        )
        .ok()
        .unwrap();
        let args = json!({"role":"review","card":{}})
            .as_object()
            .unwrap()
            .clone();
        let mut launch = crate::launch::LaunchSpec {
            max_request_tokens: None,
            split_inline_diff: false,
            argv: vec!["opencode".into()],
            cwd: state.path().into(),
            env: Default::default(),
            stdin: Some("review".into()),
            prompt: Some("review".into()),
            events: None,
            warnings: None,
        };
        let review = crate::prompt::prepare_launch(&args, &leg, &mut launch, false)
            .ok()
            .unwrap();
        let slot = crate::seat_admission::reserve_now(state.path(), &leg, &policy)
            .ok()
            .unwrap();
        let slot_file = state
            .path()
            .join("seat-slots/fixture-fallback")
            .join(slot.as_ref().unwrap().token());
        if break_save {
            // A non-empty directory where the record file goes: the atomic save cannot replace it.
            let file = record.path().to_owned();
            std::fs::remove_file(&file).unwrap();
            std::fs::create_dir_all(file.join("blocker")).unwrap();
        }
        let advanced = advance(
            &mut record,
            args,
            &previous,
            1,
            leg.clone(),
            (new_job_id(), launch, review),
            None,
            slot,
        )
        .is_ok();
        let held = slot_file.exists();
        (state, json!(held), advanced)
    }

    #[test]
    fn a_saved_retry_keeps_its_slot_and_a_failed_save_frees_it() {
        let (_state, held, advanced) = advance_with_slot(false);
        assert!(advanced);
        assert_eq!(held, json!(true), "the saved record holds the slot");
        let (_state, held, advanced) = advance_with_slot(true);
        assert!(!advanced, "the save failed");
        assert_eq!(
            held,
            json!(false),
            "a failed save must not leave the slot occupied"
        );
    }
}
