//! Stale-gate alert: a pull request whose required run is green while nobody has asked for the GO.
//!
//! plangraph wait gate-alert --pr N [--repo R] [--workflow W] [--grace SECS]
//! Exit 0 when there is nothing to say (no green run of the head yet, a GO request exists, or the
//! run turned green inside the grace, default 900 s), 1 with an `ALERT` line, 2 on a usage or `gh`
//! error. A GO request is a pull request comment line `GO-REQ ...` that names the current head.
use crate::wait::Options;
use crate::wait_ci::{gh, latest_run, newest_run, pr_head, repo_args, Run};
use chrono::{DateTime, Utc};
use serde_json::Value;
use std::time::Duration;

const DEFAULT_GRACE: Duration = Duration::from_secs(900);
const GH_BUDGET: Duration = Duration::from_secs(60);

#[derive(Debug, PartialEq)]
pub struct Alert {
    pub run: u64,
    pub head: String,
    pub green_for_secs: u64,
}

/// Whether a comment asks for the GO of exactly `head`.
pub fn requests_go(comments: &[String], head: &str) -> bool {
    comments.iter().any(|body| {
        body.lines()
            .any(|line| line.trim_start().starts_with("GO-REQ") && line.contains(head))
    })
}

/// The alert for a head whose newest run is green for longer than `grace` with no GO request.
pub fn stale_gate(
    runs: &[Run],
    head: &str,
    go_requested: bool,
    now: DateTime<Utc>,
    grace: Duration,
) -> Option<Alert> {
    let run = newest_run(runs, Some(head))?;
    if go_requested
        || run.status != "completed"
        || !matches!(run.conclusion.as_str(), "success" | "skipped" | "neutral")
    {
        return None;
    }
    let finished = DateTime::parse_from_rfc3339(&run.updated_at).ok()?;
    let age = now.signed_duration_since(finished).to_std().ok()?;
    (age > grace).then(|| Alert {
        run: run.id,
        head: head.to_owned(),
        green_for_secs: age.as_secs(),
    })
}

fn comments(options: &Options, pr: &str) -> Result<Vec<String>, String> {
    let mut args = vec![
        "pr".into(),
        "view".into(),
        pr.to_owned(),
        "--json".into(),
        "comments".into(),
    ];
    args.extend(repo_args(options));
    let value: Value =
        serde_json::from_str(&gh(&args, GH_BUDGET)?).map_err(|_| "invalid gh JSON".to_string())?;
    Ok(value["comments"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row["body"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default())
}

fn check(options: &Options, pr: &str, grace: Duration) -> Result<Option<Alert>, String> {
    let (branch, head) = pr_head(options, pr, GH_BUDGET)?;
    let run = latest_run(options, &branch, &head, GH_BUDGET)?;
    let go = requests_go(&comments(options, pr)?, &head);
    Ok(stale_gate(run.as_slice(), &head, go, Utc::now(), grace))
}

pub fn run(args: &[String]) -> i32 {
    let mut options = Options {
        workflow: "tests.yml".into(),
        ..Options::default()
    };
    let mut grace = DEFAULT_GRACE;
    let mut i = 0;
    while i < args.len() {
        let Some(value) = args.get(i + 1).cloned() else {
            return usage();
        };
        match args[i].as_str() {
            "--pr" => options.pr = Some(value),
            "--repo" => options.repo = Some(value),
            "--workflow" => options.workflow = value,
            "--grace" => match value.parse() {
                Ok(secs) => grace = Duration::from_secs(secs),
                Err(_) => return usage(),
            },
            _ => return usage(),
        }
        i += 2;
    }
    let Some(pr) = options.pr.clone() else {
        return usage();
    };
    match check(&options, &pr, grace) {
        Ok(Some(alert)) => {
            println!(
                "ALERT green-without-go pr={pr} head={} run={} green_for={}s",
                alert.head, alert.run, alert.green_for_secs
            );
            1
        }
        Ok(None) => {
            println!("OK gate pr={pr}");
            0
        }
        Err(error) => {
            eprintln!("wait gate-alert: {error}");
            2
        }
    }
}

fn usage() -> i32 {
    eprintln!("usage: plangraph wait gate-alert --pr N [--repo R] [--workflow W] [--grace SECS]");
    2
}

#[cfg(test)]
#[path = "gate_alert_tests.rs"]
mod tests;
