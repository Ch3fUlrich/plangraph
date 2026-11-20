//! Group oversized inline review diffs by top-level directory and aggregate group seat verdicts.
use crate::{
    errors::ToolError,
    launch::LaunchSpec,
    prompt::{
        diff_cap, leg_max_request_tokens, oversized_reason, resolve_inline_diff_target,
        InlineDiffError,
    },
    prompt_git::{list_changed_files, read_diff},
};
use pg_worker_core::{
    hosts::RepoEntry,
    review_split as core,
    run::{record::valid_run_id, record_io::atomic_write_private},
    workspace::DiffRef,
};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path};

pub(crate) use pg_worker_core::review_split::{group_banner, parse_verdict_text, SeatVerdict};

#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn plan_groups_from_measured(
    files: Vec<(String, Vec<u8>)>,
    leg: &Value,
    original: &str,
    mandatory: bool,
) -> Result<Vec<Vec<String>>, InlineDiffError> {
    core::plan_groups_from_measured(files, leg, original, mandatory)
        .map_err(InlineDiffError::Oversized)
}

pub(crate) fn plan_groups_for_repo(
    repo: &RepoEntry,
    diff: &DiffRef,
    leg: &Value,
    original: &str,
    mandatory: bool,
) -> Result<Vec<Vec<String>>, InlineDiffError> {
    let files = list_changed_files(repo, diff)?;
    if files.is_empty() {
        return Ok(Vec::new());
    }
    let cap = diff_cap(leg);
    let max_tokens = leg_max_request_tokens(leg);
    let mut measured = Vec::with_capacity(files.len());
    for file in files {
        let single = DiffRef {
            repo_id: diff.repo_id.clone(),
            base_sha: diff.base_sha.clone(),
            head_sha: diff.head_sha.clone(),
            paths: Some(vec![file.clone()]),
        };
        let bytes = read_diff(repo, &single, cap).map_err(|err| match err {
            InlineDiffError::TooLarge => oversized_reason(cap, max_tokens, Some(&file)),
            other => other,
        })?;
        measured.push((file, bytes));
    }
    plan_groups_from_measured(measured, leg, original, mandatory)
}

pub(crate) fn plan_review_groups(
    args: &Value,
    leg: &Value,
    launch: &LaunchSpec,
    repos: &BTreeMap<String, RepoEntry>,
    enabled: bool,
) -> Result<Option<Vec<Vec<String>>>, InlineDiffError> {
    let Some((diff, repo, text, mandatory)) =
        resolve_inline_diff_target(args, leg, launch, repos, enabled)?
    else {
        return Ok(None);
    };
    let groups = plan_groups_for_repo(repo, &diff, leg, &text, mandatory)?;
    Ok((!groups.is_empty()).then_some(groups))
}

pub(crate) fn seat_verdict(state: &Path, record: &Value) -> SeatVerdict {
    let status = record["status"].as_str().unwrap_or("");
    if matches!(status, "running" | "queued" | "dry_run") {
        return SeatVerdict::Pending;
    }
    if status != "completed" || record["exit_code"].as_i64() != Some(0) {
        return SeatVerdict::Fail;
    }
    if let Some(v) = record["verdict"].as_str() {
        match v.to_ascii_uppercase().as_str() {
            "PASS" | "ACCEPT" => return SeatVerdict::Pass,
            "REQUEST_CHANGES" => return SeatVerdict::RequestChanges,
            "FAIL" | "HALT" | "NO_VERDICT" => return SeatVerdict::Fail,
            _ => {}
        }
    }
    if let Ok(text) = crate::review_artifacts::review_text(state, record) {
        if text == "Full rendered review unavailable or unsupported.\nVERDICT: HALT\n" {
            if let Some(tail) = record["stdout_tail"].as_str() {
                if matches!(parse_verdict_text(tail), Some(SeatVerdict::RequestChanges)) {
                    return SeatVerdict::RequestChanges;
                }
            }
            return SeatVerdict::Fail;
        }
        return parse_verdict_text(&text).unwrap_or(SeatVerdict::Fail);
    }
    SeatVerdict::Fail
}

pub(crate) fn aggregate_group_seats(
    state: &Path,
    seat_group_id: &str,
    records: &[Value],
    dry: bool,
) -> Value {
    let verdicts: Vec<SeatVerdict> = records.iter().map(|r| seat_verdict(state, r)).collect();
    let any_pending = !dry && verdicts.contains(&SeatVerdict::Pending);
    let any_request_changes = verdicts.contains(&SeatVerdict::RequestChanges);
    let all_pass = !records.is_empty() && verdicts.iter().all(|v| *v == SeatVerdict::Pass);

    let groups: Vec<Value> = records
        .iter()
        .zip(&verdicts)
        .map(|(record, verdict)| {
            json!({
                "run_id": record["run_id"],
                "index": record["review_group"]["index"],
                "total": record["review_group"]["total"],
                "paths": record["review_group"]["paths"],
                "status": record["status"],
                "verdict": if dry { Value::Null } else { json!(verdict.as_str()) },
            })
        })
        .collect();

    let (status, exit_code, verdict, passed, reason) = if dry {
        ("dry_run", json!(0), Value::Null, false, Value::Null)
    } else if any_pending {
        (
            "running",
            Value::Null,
            if any_request_changes {
                json!("REQUEST_CHANGES")
            } else {
                Value::Null
            },
            false,
            Value::Null,
        )
    } else if all_pass {
        ("completed", json!(0), json!("PASS"), true, Value::Null)
    } else if any_request_changes {
        (
            "failed",
            json!(1),
            json!("REQUEST_CHANGES"),
            false,
            json!("review_request_changes"),
        )
    } else {
        (
            "failed",
            json!(1),
            json!("FAIL"),
            false,
            json!("review_failed"),
        )
    };

    let mut out = json!({
        "seat_group": seat_group_id,
        "role": "review",
        "status": status,
        "exit_code": exit_code,
        "verdict": verdict,
        "passed": passed,
        "seats": records,
        "groups": groups,
    });
    if !reason.is_null() {
        out["reason"] = reason;
    }
    out
}

pub(crate) fn save_seat_group(state: &Path, envelope: &Value) -> Result<(), ToolError> {
    let id = envelope["seat_group"]
        .as_str()
        .filter(|s| {
            s.starts_with("seats-")
                && s["seats-".len()..]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric())
                && valid_run_id(&format!("run-{}", &s["seats-".len()..]))
        })
        .ok_or_else(|| ToolError::Internal("invalid seat group id".into()))?;
    let runs: Vec<Value> = envelope["seats"]
        .as_array()
        .map(|s| s.iter().map(|r| r["run_id"].clone()).collect())
        .unwrap_or_default();
    let payload = json!({
        "seat_group": id,
        "runs": runs,
        "groups": envelope["groups"],
        "status": envelope["status"],
        "verdict": envelope["verdict"],
        "passed": envelope["passed"],
    });
    atomic_write_private(
        &state.join("seat-groups").join(format!("{id}.json")),
        payload.to_string().as_bytes(),
    )
    .map_err(|_| ToolError::Internal("could not save seat group".into()))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn spawn_rust_split_groups(
    runs: &crate::runs::Runs,
    state: &Path,
    args: &crate::placement::Args,
    host: &str,
    role: &Value,
    plan: &Value,
    leg: &Value,
    cwd: &Path,
    dry: bool,
    groups: &[Vec<String>],
    launch: &mut impl FnMut(&Path, &Value) -> Result<LaunchSpec, ToolError>,
) -> Result<Value, ToolError> {
    use pg_worker_core::run::record::{new_job_id, new_run_id};
    let seat_group_id = format!("seats-{}", &new_run_id()["run-".len()..]);
    let total = groups.len();
    let mut run_ids = Vec::with_capacity(total);
    for (idx, group_paths) in groups.iter().enumerate() {
        let index = idx + 1;
        let (run_id, job_id) = (new_run_id(), new_job_id());
        let mut spec = launch(
            state,
            &json!({
                "run_id":run_id,"job_id":job_id,"role":role,"dry_run":dry,"leg":leg,
                "writer_families":args.get("writer_families").cloned().unwrap_or(json!([])),
                "review_of":args.get("review_of"),
                "prompt":args["prompt"],"card":args["card"],"cwd":cwd.to_string_lossy(),"state":state.to_string_lossy()
            }),
        )?;
        let review_head = crate::prompt::prepare_launch_group(
            args,
            leg,
            &mut spec,
            dry,
            Some((index, total, group_paths)),
        )?;
        runs.start_prepared(
            state,
            args,
            host,
            role,
            plan,
            leg,
            &run_id,
            &job_id,
            spec,
            review_head.as_deref(),
            &json!({}),
            dry,
            Some((&seat_group_id, index, total, group_paths)),
        )?;
        run_ids.push(run_id);
    }
    finish_group_seats(
        state,
        &seat_group_id,
        &run_ids,
        args.get("wait_seconds"),
        dry,
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn spawn_adapter_split_groups(
    runs: &crate::runs::Runs,
    state: &Path,
    args: &crate::placement::Args,
    host: &str,
    role: &Value,
    plan: &Value,
    leg: &Value,
    cwd: &Path,
    dry: bool,
    launch: &mut impl FnMut(&Path, &Value) -> Result<LaunchSpec, ToolError>,
) -> Result<Value, ToolError> {
    use pg_worker_core::run::record::{new_job_id, new_run_id};
    let diff_ref = &args["card"]["diff_ref"];
    let repo_root = diff_ref["repo"]
        .as_str()
        .filter(|p| Path::new(p).is_dir())
        .ok_or_else(|| ToolError::Internal("invalid diff_ref repo".into()))?;
    let base_rev = diff_ref["base"]
        .as_str()
        .ok_or_else(|| ToolError::Internal("invalid diff_ref base".into()))?;
    let head_rev = diff_ref["head"]
        .as_str()
        .ok_or_else(|| ToolError::Internal("invalid diff_ref head".into()))?;
    let base_sha = pg_worker_core::run::scrub::resolve_commit_sha(Path::new(repo_root), base_rev)
        .ok_or_else(|| ToolError::Internal("inline diff git failed".into()))?;
    let head_sha = pg_worker_core::run::scrub::resolve_commit_sha(Path::new(repo_root), head_rev)
        .ok_or_else(|| ToolError::Internal("inline diff git failed".into()))?;
    let repo_entry = RepoEntry {
        root: repo_root.to_owned(),
        worktrees_root: String::new(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
        live_dir: None,
        health_checks: None,
    };
    let diff = DiffRef {
        repo_id: "repo".into(),
        base_sha,
        head_sha,
        paths: None,
    };
    let prompt = args["prompt"].as_str().unwrap_or("");
    let groups = plan_groups_for_repo(
        &repo_entry,
        &diff,
        leg,
        prompt,
        crate::prompt::is_inline_only(leg),
    )
    .map_err(|e| match e {
        InlineDiffError::TooLarge | InlineDiffError::Oversized(_) => ToolError::Refused {
            kind: crate::errors::RefusalKind::Spawn,
            message: e.to_string(),
            legs: vec![],
        },
        other => ToolError::Internal(other.to_string()),
    })?;
    let seat_group_id = format!("seats-{}", &new_run_id()["run-".len()..]);
    let total = groups.len();
    let mut run_ids = Vec::with_capacity(total);
    for (idx, group_paths) in groups.iter().enumerate() {
        let index = idx + 1;
        let (run_id, job_id) = (new_run_id(), new_job_id());
        let mut child_args = args.clone();
        child_args["card"]["diff_ref"]["paths"] = json!(group_paths);
        let group_prompt = format!("{}{}", group_banner(index, total, group_paths), prompt);
        child_args["prompt"] = json!(group_prompt);
        let mut spec = launch(
            state,
            &json!({
                "run_id":run_id,"job_id":job_id,"role":role,"dry_run":dry,"leg":leg,
                "writer_families":child_args.get("writer_families").cloned().unwrap_or(json!([])),
                "review_of":child_args.get("review_of"),
                "prompt":child_args["prompt"],"card":child_args["card"],"cwd":cwd.to_string_lossy(),"state":state.to_string_lossy()
            }),
        )?;
        let review_head = crate::prompt::prepare_launch(&child_args, leg, &mut spec, dry)?;
        runs.start_prepared(
            state,
            &child_args,
            host,
            role,
            plan,
            leg,
            &run_id,
            &job_id,
            spec,
            review_head.as_deref(),
            &json!({}),
            dry,
            Some((&seat_group_id, index, total, group_paths)),
        )?;
        run_ids.push(run_id);
    }
    finish_group_seats(
        state,
        &seat_group_id,
        &run_ids,
        args.get("wait_seconds"),
        dry,
    )
}

fn finish_group_seats(
    state: &Path,
    seat_group_id: &str,
    run_ids: &[String],
    wait_arg: Option<&Value>,
    dry: bool,
) -> Result<Value, ToolError> {
    let records = if dry {
        run_ids
            .iter()
            .map(|id| crate::records::read_record(state, &json!(id)))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        crate::runs::wait_many(state, run_ids, crate::runs::wait_duration(wait_arg))?
    };
    let envelope = aggregate_group_seats(state, seat_group_id, &records, dry);
    save_seat_group(state, &envelope)?;
    Ok(envelope)
}

#[cfg(test)]
#[path = "review_split_tests.rs"]
mod tests;
