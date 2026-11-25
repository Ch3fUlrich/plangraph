//! Probe pull requests: closed only when a creation record binds repository, PR number, head SHA
//! and Actions run id, the PR is still open at that head, and the run's latest attempt is terminal.
//! An ordinary draft, a probe without a record and a probe whose head moved are never touched.
use super::lane_cleanup::{append, flag, state_dir};
use pg_worker_core::{host_command, lane_delete::valid_sha};
use serde::Deserialize;
use std::fs;
use std::io;
use std::process::Command;
use std::time::Duration;

const RECORDS: &str = "probe-prs.jsonl";
const CLOSED: &str = "probe-closed.jsonl";

#[derive(Deserialize)]
struct Probe {
    repo: String,
    pr: u64,
    head: String,
    run_id: u64,
}
#[derive(Deserialize)]
struct PrView {
    state: String,
    #[serde(rename = "headRefOid")]
    head: String,
}
#[derive(Deserialize)]
struct RunView {
    status: String,
    #[serde(rename = "headSha")]
    head: String,
}

fn number(args: &[String], name: &str) -> Result<u64, String> {
    flag(args, name)
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| format!("{name} needs a number"))
}

/// The producer of the creation record: call it where the probe PR and its run are created.
pub(super) fn record(args: &[String]) -> Result<(), String> {
    let state = state_dir(args)?;
    let repo = flag(args, "--gh-repo")
        .filter(|r| r.contains('/'))
        .ok_or("--gh-repo OWNER/NAME")?;
    let head = flag(args, "--head")
        .filter(|h| valid_sha(h))
        .ok_or("--head needs a 40-hex SHA")?;
    let row = serde_json::json!({"repo": repo, "pr": number(args, "--pr")?, "head": head,
        "run_id": number(args, "--run-id")?});
    append(&state.join(RECORDS), &format!("{row}\n")).map_err(|e| e.to_string())
}

fn gh(args: &[&str]) -> io::Result<Vec<u8>> {
    host_command::output(Command::new("gh").args(args), Duration::from_secs(60))
}

/// `None` when the PR is open at the recorded head and the bound run (same head) has completed.
fn evidence(probe: &Probe) -> Result<Option<&'static str>, String> {
    let (pr, run) = (probe.pr.to_string(), probe.run_id.to_string());
    let view: PrView = serde_json::from_slice(
        &gh(&[
            "pr",
            "view",
            &pr,
            "--repo",
            &probe.repo,
            "--json",
            "state,headRefOid",
        ])
        .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    if view.state != "OPEN" {
        return Ok(Some("not-open"));
    }
    if view.head != probe.head {
        return Ok(Some("head-moved"));
    }
    let run: RunView = serde_json::from_slice(
        &gh(&[
            "run",
            "view",
            &run,
            "--repo",
            &probe.repo,
            "--json",
            "status,headSha",
        ])
        .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    if run.head != probe.head {
        return Ok(Some("run-head-differs"));
    }
    Ok((run.status != "completed").then_some("run-active"))
}

fn closed_before(state: &std::path::Path, repo: &str, pr: u64) -> bool {
    fs::read_to_string(state.join(CLOSED)).is_ok_and(|text| {
        text.lines().any(|l| {
            serde_json::from_str::<serde_json::Value>(l)
                .is_ok_and(|v| v["repo"] == repo && v["pr"] == pr)
        })
    })
}

pub(super) fn close(args: &[String]) -> Result<i32, String> {
    let state = state_dir(args)?;
    let scope = flag(args, "--gh-repo").ok_or("--gh-repo OWNER/NAME")?;
    let apply = args.iter().any(|a| a == "--apply");
    let text = match fs::read_to_string(state.join(RECORDS)) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.to_string()),
    };
    let mut failed = false;
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let probe: Probe = serde_json::from_str(line).map_err(|e| format!("probe record: {e}"))?;
        if probe.repo != scope || closed_before(&state, &probe.repo, probe.pr) {
            continue;
        }
        let pr = probe.pr.to_string();
        let run = probe.run_id.to_string();
        // Evidence is read again right before the close: a push or rerun in between keeps the PR.
        let verdict = match evidence(&probe) {
            Ok(None) if apply => evidence(&probe),
            other => other,
        };
        match verdict {
            Ok(None) if apply => {
                let note = "Closed by lane-cleanup: the probe run ended and the head is unchanged.";
                match gh(&["pr", "close", &pr, "--repo", &probe.repo, "--comment", note]) {
                    Ok(_) => {
                        println!("probe #{pr} run {run} closed");
                        let row = serde_json::json!({"repo": probe.repo, "pr": probe.pr,
                            "head": probe.head, "run_id": probe.run_id});
                        append(&state.join(CLOSED), &format!("{row}\n"))
                            .map_err(|e| e.to_string())?;
                    }
                    Err(e) => {
                        failed = true;
                        println!("probe #{pr} close refused ({e})");
                    }
                }
            }
            Ok(None) => println!("probe #{pr} run {run} would-close"),
            Ok(Some(why)) => println!("probe #{pr} run {run} kept ({why})"),
            Err(why) => println!("probe #{pr} run {run} kept (evidence unavailable: {why})"),
        }
    }
    Ok(if failed { 3 } else { 0 })
}
