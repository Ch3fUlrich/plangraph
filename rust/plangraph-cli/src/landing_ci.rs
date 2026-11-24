use crate::feeder::Transport;
use chrono::DateTime;
use serde_json::{json, Value};
use std::{io, process::Command};

fn github(endpoint: &str, paginated: bool) -> Result<Value, String> {
    let mut command = Command::new("gh");
    command.args(["api", endpoint]);
    if paginated {
        command.args(["--paginate", "--slurp"]);
    }
    let output = command.output().map_err(|_| "cannot run gh")?;
    if !output.status.success() {
        return Err("GitHub Actions timing request failed".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "invalid GitHub timing response".into())
}

fn millis(value: &Value) -> Result<i64, String> {
    let text = value.as_str().ok_or("missing GitHub timing")?;
    DateTime::parse_from_rfc3339(text)
        .map(|stamp| stamp.timestamp_millis())
        .map_err(|_| "invalid GitHub timestamp".into())
}

pub fn import(keeper: &dyn Transport, args: &[String]) -> Result<Value, String> {
    let [repo, batch, head, run_id, attempt, flags @ ..] = args else {
        return Err(
            "usage: plangraph landing ci-import OWNER/REPO BATCH HEAD RUN ATTEMPT [--fixture]"
                .into(),
        );
    };
    let mut repo_parts = repo.split('/');
    if repo_parts.clone().count() != 2
        || !repo_parts.all(|p| {
            !p.is_empty()
                && p.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        })
        || head.len() != 40
        || !head.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err("invalid GitHub repository or head".into());
    }
    let run_id: u64 = run_id.parse().map_err(|_| "invalid run id")?;
    let attempt: u64 = attempt.parse().map_err(|_| "invalid run attempt")?;
    if run_id == 0 || !(1..1000).contains(&attempt) {
        return Err("invalid run id or run attempt".into());
    }
    let stage_attempt = run_id
        .checked_mul(1000)
        .and_then(|id| id.checked_add(attempt))
        .filter(|id| *id <= i64::MAX as u64)
        .ok_or("GitHub run id too large")?;
    let (run, jobs, fixture) = match flags {
        [flag] if flag == "--fixture" => {
            let data: Value =
                serde_json::from_reader(io::stdin().lock()).map_err(|_| "invalid fixture JSON")?;
            let jobs = data["jobs"]
                .as_array()
                .ok_or("fixture has no jobs")?
                .clone();
            (data["run"].clone(), jobs, true)
        }
        [] => {
            let url = format!("repos/{repo}/actions/runs/{run_id}/attempts/{attempt}");
            let run = github(&url, false)?;
            let pages = github(&format!("{url}/jobs?per_page=100"), true)?;
            let mut jobs = Vec::new();
            for page in pages.as_array().ok_or("missing GitHub job pages")? {
                jobs.extend(
                    page["jobs"]
                        .as_array()
                        .ok_or("missing GitHub jobs")?
                        .iter()
                        .cloned(),
                );
            }
            (run, jobs, false)
        }
        _ => return Err("unknown ci-import option".into()),
    };
    if run["id"] != run_id
        || run["run_attempt"] != attempt
        || run["head_sha"] != *head
        || run["status"] != "completed"
    {
        return Err("GitHub run does not match the completed requested attempt and head".into());
    }
    let conclusion = run["conclusion"].as_str().ok_or("missing run conclusion")?;
    let start = millis(&run["run_started_at"])?;
    let end = millis(&run["updated_at"])?;
    let jobs = jobs
        .iter()
        .map(|job| job_timing(job, run_id, attempt, head, end))
        .collect::<Result<Vec<_>, _>>()?;
    if end < start || jobs.is_empty() {
        return Err("completed CI run needs ordered times and at least one job".into());
    }
    let body = json!({
        "action": "ci_timings", "repo": repo, "batch_key": batch,
        "run_id": run_id, "run_attempt": attempt, "run_conclusion": conclusion,
        "stage": {"stage": "ci", "head": head, "attempt": stage_attempt,
                  "started_at_ms": start, "ended_at_ms": end,
                  "host": "github", "result": conclusion},
        "jobs": jobs,
    });
    if !fixture {
        keeper.call("land", Some(body.clone()))?;
    }
    Ok(body)
}

fn job_timing(
    job: &Value,
    run: u64,
    attempt: u64,
    head: &str,
    run_end: i64,
) -> Result<Value, String> {
    if job["run_id"] != run
        || (!job["run_attempt"].is_null() && job["run_attempt"] != attempt)
        || (!job["head_sha"].is_null() && job["head_sha"] != head)
        || job["status"] != "completed"
    {
        return Err("GitHub job is outside the requested completed run attempt".into());
    }
    let conclusion = job["conclusion"].as_str().ok_or("missing job conclusion")?;
    let (start, end) = if conclusion == "skipped"
        && job["started_at"].is_null()
        && job["completed_at"].is_null()
    {
        (run_end, run_end)
    } else {
        (millis(&job["started_at"])?, millis(&job["completed_at"])?)
    };
    let job_id = job["id"]
        .as_u64()
        .filter(|id| *id > 0)
        .ok_or("missing job id")?;
    let name = job["name"]
        .as_str()
        .filter(|name| !name.is_empty())
        .ok_or("missing job name")?;
    if end < start {
        return Err("GitHub job times are reversed".into());
    }
    Ok(
        json!({"job_id": job_id, "name": name, "conclusion": conclusion,
              "started_at_ms": start, "ended_at_ms": end, "duration_ms": end - start}),
    )
}
