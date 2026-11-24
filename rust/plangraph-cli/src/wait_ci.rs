//! `plangraph wait ci`: wait on the newest run of a head, within one deadline that also bounds every `gh` call.
use crate::wait::{Options, Outcome, DEFAULT_DEADLINE, DEFAULT_POLL};
use serde_json::Value;
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const GH_TIMEOUT: Duration = Duration::from_secs(60);
pub(crate) const GH_TIMED_OUT: &str = "gh timed out";

/// One workflow run as `gh run list` reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct Run {
    pub id: u64,
    pub status: String,
    pub conclusion: String,
    pub head_sha: String,
    /// RFC 3339 time of the last update of the run (empty when `gh` did not report it).
    pub updated_at: String,
}

/// The newest run (highest id) of `runs`, restricted to `sha` when it is known.
pub fn newest_run<'a>(runs: &'a [Run], sha: Option<&str>) -> Option<&'a Run> {
    runs.iter()
        .filter(|run| sha.is_none_or(|sha| run.head_sha == sha))
        .max_by_key(|run| run.id)
}

/// Wait until the newest run of `sha` is `completed`. `fetch` gets the time left before the
/// deadline and must stay inside it; `now` is the time since the start; `sleep` pauses and is never
/// asked for more than that remainder, so neither a slow `gh` nor a long poll moves the deadline.
pub fn wait_ci(
    fetch: &mut dyn FnMut(Duration) -> Result<Vec<Run>, String>,
    sleep: &mut dyn FnMut(Duration),
    now: &mut dyn FnMut() -> Duration,
    deadline: Duration,
    poll: Duration,
    sha: Option<&str>,
) -> Outcome {
    let mut last = String::from("no poll finished");
    loop {
        let remaining = deadline.saturating_sub(now());
        if remaining.is_zero() {
            return timeout(deadline, &last);
        }
        match fetch(remaining) {
            Ok(runs) => match newest_run(&runs, sha) {
                Some(run) if run.status == "completed" => {
                    let detail = format!("run={} conclusion={}", run.id, run.conclusion);
                    // The conclusion of the run decides; jobs skipped inside a green run do not matter.
                    return if matches!(run.conclusion.as_str(), "success" | "skipped" | "neutral") {
                        Outcome::Green { detail }
                    } else {
                        Outcome::Red { detail }
                    };
                }
                Some(run) => last = format!("run={} status={}", run.id, run.status),
                None => last = "no run seen for this head".into(),
            },
            Err(error) => last = format!("gh: {error}"),
        }
        let remaining = deadline.saturating_sub(now());
        if remaining.is_zero() {
            return timeout(deadline, &last);
        }
        sleep(poll.min(remaining));
    }
}

fn timeout(deadline: Duration, last: &str) -> Outcome {
    Outcome::Timeout {
        detail: format!("deadline {}s: still pending: {last}", deadline.as_secs()),
    }
}

/// `gh`, killed after `budget` (at most `GH_TIMEOUT`): a hung `gh` must not outlive the deadline.
pub(crate) fn gh(args: &[String], budget: Duration) -> Result<String, String> {
    let limit = budget.min(GH_TIMEOUT);
    let mut child = Command::new("gh")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "cannot execute gh".to_string())?;
    let mut stdout = child.stdout.take().ok_or("no gh stdout")?;
    // The output is read on a thread and awaited with the same limit: a descendant of `gh` that
    // keeps the pipe open after `gh` exited must not hold the wait past its deadline.
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut text = String::new();
        let _ = sender.send(stdout.read_to_string(&mut text).map(|_| text));
    });
    let started = Instant::now();
    let status = loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) => break status,
            None if started.elapsed() > limit => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(GH_TIMED_OUT.into());
            }
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    };
    let text = receiver
        .recv_timeout(
            limit
                .saturating_sub(started.elapsed())
                .max(Duration::from_millis(50)),
        )
        .map_err(|_| GH_TIMED_OUT.to_string())?
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok(text)
    } else {
        Err("gh request failed".into())
    }
}

pub(crate) fn runs_from_json(text: &str) -> Result<Vec<Run>, String> {
    let rows: Vec<Value> = serde_json::from_str(text).map_err(|_| "invalid gh JSON".to_string())?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some(Run {
                id: row["databaseId"].as_u64()?,
                status: row["status"].as_str()?.to_owned(),
                conclusion: row["conclusion"].as_str().unwrap_or("").to_owned(),
                head_sha: row["headSha"].as_str().unwrap_or("").to_owned(),
                updated_at: row["updatedAt"].as_str().unwrap_or("").to_owned(),
            })
        })
        .collect())
}

pub(crate) fn repo_args(options: &Options) -> Vec<String> {
    options
        .repo
        .iter()
        .flat_map(|repo| ["--repo".to_string(), repo.clone()])
        .collect()
}

/// Branch and head of `--pr N`, from the pull request itself (never from its check rollup).
pub(crate) fn pr_head(
    options: &Options,
    pr: &str,
    budget: Duration,
) -> Result<(String, String), String> {
    let mut args = vec![
        "pr".into(),
        "view".into(),
        pr.to_owned(),
        "--json".into(),
        "headRefName,headRefOid".into(),
    ];
    args.extend(repo_args(options));
    let value: Value =
        serde_json::from_str(&gh(&args, budget)?).map_err(|_| "invalid gh JSON".to_string())?;
    match (value["headRefName"].as_str(), value["headRefOid"].as_str()) {
        (Some(branch), Some(sha)) => Ok((branch.to_owned(), sha.to_owned())),
        _ => Err("pull request has no head".into()),
    }
}

/// The commit a branch points at now: a `--branch` wait never accepts a run of an older commit.
fn branch_tip(options: &Options, branch: &str, budget: Duration) -> Result<String, String> {
    let repo = options.repo.as_deref().unwrap_or("{owner}/{repo}");
    let args = [
        "api".to_string(),
        format!("repos/{repo}/commits/{branch}"),
        "--jq".into(),
        ".sha".into(),
    ];
    let sha = gh(&args, budget)?.trim().to_owned();
    if sha.is_empty() {
        return Err("branch has no tip".into());
    }
    Ok(sha)
}

/// One deadline starts here, before any lookup, and bounds the lookups, every poll and every sleep.
pub(crate) fn run_ci(options: &Options) -> Result<(Outcome, String, Duration), String> {
    let start = Instant::now();
    let deadline = options.deadline.unwrap_or(DEFAULT_DEADLINE);
    let left = || deadline.saturating_sub(start.elapsed());
    let name = |branch: &str| {
        format!(
            "ci-{}",
            options
                .pr
                .as_deref()
                .map_or_else(|| branch.to_owned(), |pr| format!("pr{pr}"))
        )
    };
    let lookup = match (&options.pr, &options.branch) {
        (Some(pr), None) => pr_head(options, pr, left()).map(|(branch, sha)| {
            let sha = options.sha.clone().unwrap_or(sha);
            (branch, sha)
        }),
        (None, Some(branch)) => match &options.sha {
            Some(sha) => Ok((branch.clone(), sha.clone())),
            None => branch_tip(options, branch, left()).map(|sha| (branch.clone(), sha)),
        },
        _ => return Err("wait ci needs exactly one of --pr and --branch".into()),
    };
    let (branch, sha) = match lookup {
        Ok(found) => found,
        Err(error) if error == GH_TIMED_OUT || left().is_zero() => {
            let branch = options.branch.clone().unwrap_or_default();
            return Ok((
                timeout(deadline, &format!("head lookup: {error}")),
                name(&branch),
                start.elapsed(),
            ));
        }
        Err(error) => return Err(error),
    };
    let mut list = vec![
        "run".into(),
        "list".into(),
        "--branch".into(),
        branch.clone(),
        "--workflow".into(),
        options.workflow.clone(),
        "--limit".into(),
        "30".into(),
        "--json".into(),
        "databaseId,status,conclusion,headSha,updatedAt".into(),
    ];
    list.extend(repo_args(options));
    let outcome = wait_ci(
        &mut |budget| runs_from_json(&gh(&list, budget)?),
        &mut std::thread::sleep,
        &mut || start.elapsed(),
        deadline,
        options.poll.unwrap_or(DEFAULT_POLL),
        Some(&sha),
    );
    Ok((outcome, name(&branch), start.elapsed()))
}

#[cfg(test)]
#[path = "wait_ci_tests.rs"]
mod tests;
