//! `plangraph wait`: the one way to wait for CI, a writer or a seat, always with a deadline.
//!
//! plangraph wait ci (--pr N | --branch B) [--sha S] [--repo R] [--workflow W] [--deadline SECS]
//! plangraph wait files [--status FILE]... [--result FILE]... [--deadline SECS]
//!
//! `ci` ends on the status of the newest run of the workflow for that head (`completed`), never on
//! the check rollup of the pull request: skipped and status-less entries of a rollup never complete.
//! plangraph wait lane-alert --status FILE [--remote R] [--branch B] [--grace SECS]   (see lane_alert.rs)
//! plangraph wait gate-alert --pr N [--repo R] [--workflow W] [--grace SECS]   (see gate_alert.rs)
//! Every wait writes `<name>.json` under the job directory (`--result-dir`, `$CLAUDE_JOB_DIR/waits`,
//! else `~/.local/state/plangraph/waits`) so that a successor reads the outcome without waiting again.
//! Exit codes: 0 green, 1 red, 2 usage or error, 3 deadline (the output names what was still pending).
use crate::atomic;
use crate::wait_ci::run_ci;
use crate::wait_files::run_files;
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage: plangraph wait ci (--pr N | --branch B) [--sha S] [--repo R] [--workflow W] [--deadline SECS] [--poll SECS] [--name N] [--result-dir D]\n       plangraph wait files [--status FILE]... [--result FILE]... [--deadline SECS] [--poll SECS] [--name N] [--result-dir D]";
pub(crate) const DEFAULT_DEADLINE: Duration = Duration::from_secs(3600);
pub(crate) const DEFAULT_POLL: Duration = Duration::from_secs(20);

#[derive(Debug, PartialEq)]
pub enum Outcome {
    Green { detail: String },
    Red { detail: String },
    Timeout { detail: String },
}

impl Outcome {
    pub(crate) fn code(&self) -> i32 {
        match self {
            Outcome::Green { .. } => 0,
            Outcome::Red { .. } => 1,
            Outcome::Timeout { .. } => 3,
        }
    }
    pub(crate) fn word(&self) -> &'static str {
        match self {
            Outcome::Green { .. } => "green",
            Outcome::Red { .. } => "red",
            Outcome::Timeout { .. } => "timeout",
        }
    }
    pub(crate) fn detail(&self) -> &str {
        match self {
            Outcome::Green { detail } | Outcome::Red { detail } | Outcome::Timeout { detail } => {
                detail
            }
        }
    }
}

#[derive(Serialize)]
struct Record<'a> {
    kind: &'a str,
    target: &'a str,
    outcome: &'a str,
    detail: &'a str,
    elapsed_secs: u64,
    finished_unix: u64,
}

fn result_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(dir) = explicit {
        return dir.to_path_buf();
    }
    if let Some(job) = std::env::var_os("CLAUDE_JOB_DIR") {
        return Path::new(&job).join("waits");
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".local/state/plangraph/waits")
}

fn safe_name(name: &str) -> String {
    name.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "-._".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn write_record(
    dir: &Path,
    name: &str,
    kind: &str,
    target: &str,
    outcome: &Outcome,
    elapsed: Duration,
) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("result dir: {e}"))?;
    let record = Record {
        kind,
        target,
        outcome: outcome.word(),
        detail: outcome.detail(),
        elapsed_secs: elapsed.as_secs(),
        finished_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs()),
    };
    let path = dir.join(format!("{}.json", safe_name(name)));
    let body = serde_json::to_vec_pretty(&record).map_err(|e| e.to_string())?;
    atomic::write(&path, &body).map_err(|e| format!("result file: {e}"))?;
    Ok(path)
}

#[derive(Default)]
pub(crate) struct Options {
    pub(crate) pr: Option<String>,
    pub(crate) branch: Option<String>,
    pub(crate) sha: Option<String>,
    pub(crate) repo: Option<String>,
    pub(crate) workflow: String,
    pub(crate) statuses: Vec<PathBuf>,
    pub(crate) results: Vec<PathBuf>,
    pub(crate) deadline: Option<Duration>,
    pub(crate) poll: Option<Duration>,
    pub(crate) name: Option<String>,
    pub(crate) result_dir: Option<PathBuf>,
}

fn seconds(raw: Option<&String>, flag: &str) -> Result<Duration, String> {
    raw.and_then(|v| v.parse::<u64>().ok())
        .filter(|&n| n > 0)
        .map(Duration::from_secs)
        .ok_or_else(|| format!("{flag} needs a positive number of seconds"))
}

fn parse(args: &[String]) -> Result<Options, String> {
    let mut options = Options {
        workflow: "tests.yml".into(),
        ..Options::default()
    };
    let mut i = 0;
    while i < args.len() {
        let value = args.get(i + 1);
        let text = || {
            value
                .cloned()
                .ok_or_else(|| format!("{} needs a value", args[i]))
        };
        match args[i].as_str() {
            "--pr" => options.pr = Some(text()?),
            "--branch" => options.branch = Some(text()?),
            "--sha" => options.sha = Some(text()?),
            "--repo" => options.repo = Some(text()?),
            "--workflow" => options.workflow = text()?,
            "--status" => options.statuses.push(text()?.into()),
            "--result" => options.results.push(text()?.into()),
            "--name" => options.name = Some(text()?),
            "--result-dir" => options.result_dir = Some(text()?.into()),
            "--deadline" => options.deadline = Some(seconds(value, "--deadline")?),
            "--poll" => options.poll = Some(seconds(value, "--poll")?),
            other => return Err(format!("unknown argument {other}")),
        }
        i += 2;
    }
    Ok(options)
}

/// One `resume-digest` row per finished wait of the job: its outcome and detail, so a successor never waits again.
pub fn digest_rows(job: &Path) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(job.join("waits")) else {
        return Vec::new();
    };
    entries
        .filter_map(|entry| {
            let path = entry.ok()?.path();
            (path.extension()? == "json").then_some(path)
        })
        .map(|path| {
            let record: Value = std::fs::read_to_string(&path)
                .ok()
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or(Value::Null);
            let name = path
                .file_stem()
                .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
            format!(
                "wait {name}: {} {} ({})",
                record["outcome"].as_str().unwrap_or("unreadable"),
                record["detail"].as_str().unwrap_or(""),
                path.display()
            )
        })
        .collect()
}

pub fn cli(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("wait") {
        return None;
    }
    if args.get(1).map(String::as_str) == Some("lane-alert") {
        return Some(crate::lane_alert::run(&args[2..]));
    }
    if args.get(1).map(String::as_str) == Some("gate-alert") {
        return Some(crate::gate_alert::run(&args[2..]));
    }
    let (kind, rest) = match args.get(1).map(String::as_str) {
        Some(kind @ ("ci" | "files")) => (kind, &args[2..]),
        _ => {
            eprintln!("{USAGE}");
            return Some(2);
        }
    };
    let options = match parse(rest) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("{error}\n{USAGE}");
            return Some(2);
        }
    };
    let ran = if kind == "ci" {
        run_ci(&options)
    } else {
        run_files(&options)
    };
    let (outcome, default_name, elapsed) = match ran {
        Ok(done) => done,
        Err(error) => {
            eprintln!("wait {kind}: {error}");
            return Some(2);
        }
    };
    let name = options.name.clone().unwrap_or(default_name);
    let dir = result_dir(options.result_dir.as_deref());
    match write_record(&dir, &name, kind, &name, &outcome, elapsed) {
        Ok(path) => println!(
            "WAIT {kind} {} {} result={}",
            outcome.word(),
            outcome.detail(),
            path.display()
        ),
        Err(error) => {
            // The outcome still counts; a successor just cannot read it from a file.
            println!("WAIT {kind} {} {}", outcome.word(), outcome.detail());
            eprintln!("wait {kind}: {error}");
        }
    }
    Some(outcome.code())
}

#[cfg(test)]
#[path = "wait_tests.rs"]
mod tests;
