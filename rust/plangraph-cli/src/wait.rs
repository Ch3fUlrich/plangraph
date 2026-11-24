//! `plangraph wait`: the one way to wait for CI, a writer or a seat, always with a deadline.
//!
//! plangraph wait ci (--pr N | --branch B) [--sha S] [--repo R] [--workflow W] [--deadline SECS]
//! plangraph wait files [--status FILE]... [--result FILE]... [--deadline SECS]
//!
//! `ci` ends on the status of the newest run of the workflow for that head (`completed`), never on
//! the check rollup of the pull request: skipped and status-less entries of a rollup never complete.
//! plangraph wait lane-alert --status FILE [--remote R] [--branch B] [--grace SECS]   (see lane_alert.rs)
//! Every wait writes `<name>.json` under the job directory (`--result-dir`, `$CLAUDE_JOB_DIR/waits`,
//! else `~/.local/state/plangraph/waits`) so that a successor reads the outcome without waiting again.
//! Exit codes: 0 green, 1 red, 2 usage or error, 3 deadline (the output names what was still pending).
use crate::atomic;
use serde::Serialize;
use serde_json::Value;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage: plangraph wait ci (--pr N | --branch B) [--sha S] [--repo R] [--workflow W] [--deadline SECS] [--poll SECS] [--name N] [--result-dir D]\n       plangraph wait files [--status FILE]... [--result FILE]... [--deadline SECS] [--poll SECS] [--name N] [--result-dir D]";
const DEFAULT_DEADLINE: Duration = Duration::from_secs(3600);
const DEFAULT_POLL: Duration = Duration::from_secs(20);
const GH_TIMEOUT: Duration = Duration::from_secs(60);

/// One workflow run as `gh run list` reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct Run {
    pub id: u64,
    pub status: String,
    pub conclusion: String,
    pub head_sha: String,
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    Green { detail: String },
    Red { detail: String },
    Timeout { detail: String },
}

impl Outcome {
    fn code(&self) -> i32 {
        match self {
            Outcome::Green { .. } => 0,
            Outcome::Red { .. } => 1,
            Outcome::Timeout { .. } => 3,
        }
    }
    fn word(&self) -> &'static str {
        match self {
            Outcome::Green { .. } => "green",
            Outcome::Red { .. } => "red",
            Outcome::Timeout { .. } => "timeout",
        }
    }
    fn detail(&self) -> &str {
        match self {
            Outcome::Green { detail } | Outcome::Red { detail } | Outcome::Timeout { detail } => {
                detail
            }
        }
    }
}

/// The newest run (highest id) of `runs`, restricted to `sha` when it is known.
pub fn newest_run<'a>(runs: &'a [Run], sha: Option<&str>) -> Option<&'a Run> {
    runs.iter()
        .filter(|run| sha.is_none_or(|sha| run.head_sha == sha))
        .max_by_key(|run| run.id)
}

/// Wait until the newest run is `completed`. `now` is the time since the start; `sleep` pauses.
pub fn wait_ci(
    fetch: &mut dyn FnMut() -> Result<Vec<Run>, String>,
    sleep: &mut dyn FnMut(Duration),
    now: &mut dyn FnMut() -> Duration,
    deadline: Duration,
    poll: Duration,
    sha: Option<&str>,
) -> Outcome {
    let mut last;
    loop {
        match fetch() {
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
        if now() + poll > deadline {
            return Outcome::Timeout {
                detail: format!("deadline {}s: still pending: {last}", deadline.as_secs()),
            };
        }
        sleep(poll);
    }
}

/// State of a writer status file: `Done` at its `<NAME> DONE` line, `Blocked` at a `BLOCKED` line.
#[derive(Debug, PartialEq)]
pub enum FileState {
    Pending,
    Done,
    Blocked,
}

pub fn status_state(text: &str) -> FileState {
    for line in text.lines() {
        if line.starts_with("BLOCKED") {
            return FileState::Blocked;
        }
    }
    if text
        .lines()
        .any(|line| line.ends_with(" DONE") && !line.starts_with("DONE "))
    {
        FileState::Done
    } else {
        FileState::Pending
    }
}

/// Wait for every status file to end (`NAME DONE` or `BLOCKED`) and every result file to exist.
pub fn wait_files(
    statuses: &[PathBuf],
    results: &[PathBuf],
    sleep: &mut dyn FnMut(Duration),
    now: &mut dyn FnMut() -> Duration,
    deadline: Duration,
    poll: Duration,
) -> Outcome {
    loop {
        let mut pending = Vec::new();
        let mut blocked = Vec::new();
        for path in statuses {
            match status_state(&std::fs::read_to_string(path).unwrap_or_default()) {
                FileState::Done => {}
                FileState::Blocked => blocked.push(path.display().to_string()),
                FileState::Pending => pending.push(path.display().to_string()),
            }
        }
        for path in results {
            if std::fs::metadata(path).map_or(true, |meta| meta.len() == 0) {
                pending.push(path.display().to_string());
            }
        }
        if pending.is_empty() {
            return if blocked.is_empty() {
                Outcome::Green {
                    detail: format!(
                        "{} status, {} result files ended",
                        statuses.len(),
                        results.len()
                    ),
                }
            } else {
                Outcome::Red {
                    detail: format!("blocked: {}", blocked.join(" ")),
                }
            };
        }
        if now() + poll > deadline {
            return Outcome::Timeout {
                detail: format!(
                    "deadline {}s: still pending: {}",
                    deadline.as_secs(),
                    pending.join(" ")
                ),
            };
        }
        sleep(poll);
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

/// `gh`, killed after `GH_TIMEOUT`: a hung `gh` must not outlive the deadline.
fn gh(args: &[String]) -> Result<String, String> {
    let mut child = Command::new("gh")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "cannot execute gh".to_string())?;
    let mut stdout = child.stdout.take().ok_or("no gh stdout")?;
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        stdout.read_to_string(&mut text).map(|_| text)
    });
    let started = Instant::now();
    let status = loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) => break status,
            None if started.elapsed() > GH_TIMEOUT => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("gh timed out".into());
            }
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    };
    let text = reader
        .join()
        .map_err(|_| "gh reader panicked")?
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok(text)
    } else {
        Err("gh request failed".into())
    }
}

fn runs_from_json(text: &str) -> Result<Vec<Run>, String> {
    let rows: Vec<Value> = serde_json::from_str(text).map_err(|_| "invalid gh JSON".to_string())?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some(Run {
                id: row["databaseId"].as_u64()?,
                status: row["status"].as_str()?.to_owned(),
                conclusion: row["conclusion"].as_str().unwrap_or("").to_owned(),
                head_sha: row["headSha"].as_str().unwrap_or("").to_owned(),
            })
        })
        .collect())
}

#[derive(Default)]
struct Options {
    pr: Option<String>,
    branch: Option<String>,
    sha: Option<String>,
    repo: Option<String>,
    workflow: String,
    statuses: Vec<PathBuf>,
    results: Vec<PathBuf>,
    deadline: Option<Duration>,
    poll: Option<Duration>,
    name: Option<String>,
    result_dir: Option<PathBuf>,
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

fn repo_args(options: &Options) -> Vec<String> {
    options
        .repo
        .iter()
        .flat_map(|repo| ["--repo".to_string(), repo.clone()])
        .collect()
}

/// Branch and head of `--pr N`, from the pull request itself (never from its check rollup).
fn pr_head(options: &Options, pr: &str) -> Result<(String, String), String> {
    let mut args = vec![
        "pr".into(),
        "view".into(),
        pr.to_owned(),
        "--json".into(),
        "headRefName,headRefOid".into(),
    ];
    args.extend(repo_args(options));
    let value: Value =
        serde_json::from_str(&gh(&args)?).map_err(|_| "invalid gh JSON".to_string())?;
    match (value["headRefName"].as_str(), value["headRefOid"].as_str()) {
        (Some(branch), Some(sha)) => Ok((branch.to_owned(), sha.to_owned())),
        _ => Err("pull request has no head".into()),
    }
}

fn run_ci(options: &Options) -> Result<(Outcome, String, Duration), String> {
    let (branch, sha) = match (&options.pr, &options.branch) {
        (Some(pr), None) => {
            let (branch, sha) = pr_head(options, pr)?;
            (branch, options.sha.clone().or(Some(sha)))
        }
        (None, Some(branch)) => (branch.clone(), options.sha.clone()),
        _ => return Err("wait ci needs exactly one of --pr and --branch".into()),
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
        "databaseId,status,conclusion,headSha".into(),
    ];
    list.extend(repo_args(options));
    let start = Instant::now();
    let outcome = wait_ci(
        &mut || runs_from_json(&gh(&list)?),
        &mut std::thread::sleep,
        &mut || start.elapsed(),
        options.deadline.unwrap_or(DEFAULT_DEADLINE),
        options.poll.unwrap_or(DEFAULT_POLL),
        sha.as_deref(),
    );
    Ok((
        outcome,
        format!(
            "ci-{}",
            options.pr.as_deref().map_or(branch, |pr| format!("pr{pr}"))
        ),
        start.elapsed(),
    ))
}

fn run_files(options: &Options) -> Result<(Outcome, String, Duration), String> {
    if options.statuses.is_empty() && options.results.is_empty() {
        return Err("wait files needs at least one --status or --result".into());
    }
    let start = Instant::now();
    let outcome = wait_files(
        &options.statuses,
        &options.results,
        &mut std::thread::sleep,
        &mut || start.elapsed(),
        options.deadline.unwrap_or(DEFAULT_DEADLINE),
        options.poll.unwrap_or(DEFAULT_POLL),
    );
    let first = options
        .statuses
        .iter()
        .chain(&options.results)
        .next()
        .and_then(|p| p.file_name());
    Ok((
        outcome,
        format!(
            "files-{}",
            first.map_or_else(String::new, |n| n.to_string_lossy().into_owned())
        ),
        start.elapsed(),
    ))
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
