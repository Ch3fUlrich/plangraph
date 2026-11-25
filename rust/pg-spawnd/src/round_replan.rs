//! Durable replan job for a writer loop the round stop rule ended (`no_progress`, `round_cap`).
//!
//! `enqueue` writes one job file per item and PR when the admission refuses. `run_pending`
//! walks the job files and drives each through four persisted steps: plan the slices (the
//! strongest planning leg of the route groups the PR's changed files, `SLICE_TARGET_LINES`
//! changed lines per slice, `Cargo.lock` and generated files excluded, every open finding
//! verbatim on every slice), define the slice items, close the PR unmerged, send one routing
//! line (under a key that stays the same on every retry). Every step is saved before the next
//! one starts and skipped when saved, so a replayed trigger or a retry after a crash never
//! repeats a close or a definition, and the report is retried until its delivery is acknowledged.
use crate::{errors::ToolError, placement::parameter};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

#[cfg(test)]
#[path = "round_replan_tests.rs"]
pub(crate) mod tests;

/// Rule 3.35: the target size of one slice in changed lines; a target, not a gate.
pub(crate) const SLICE_TARGET_LINES: u64 = 1500;
/// Refusal reasons of the admission that start a replan.
pub(crate) const TRIGGERS: [&str; 2] = ["no_progress", "round_cap"];

/// One leg of the spawner route as the job sees it.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Leg {
    pub name: String,
    pub intelligence: f64,
    pub planning: bool,
    pub available: bool,
}

/// One file of the PR diff with its changed lines (added plus deleted).
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct FileChange {
    pub path: String,
    pub lines: u64,
}

/// What the planning leg is given. The production planner adopts `proposal`; a leg-backed one
/// reads the rest.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) struct PlanInput<'a> {
    pub item: &'a str,
    pub item_text: &'a str,
    pub files: &'a [FileChange],
    pub findings: &'a [String],
    pub target_lines: u64,
    /// The deterministic grouping of `files`; a leg may return a better one.
    pub proposal: &'a [Vec<String>],
}

/// One slice item to define.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Slice {
    pub key: String,
    pub parent: String,
    pub files: Vec<String>,
    pub lines: u64,
    /// Every open finding of the replanned PR, verbatim.
    pub findings: Vec<String>,
}

/// The outside world of the job; every call must be safe to repeat.
pub(crate) trait Ports {
    fn legs(&self) -> Result<Vec<Leg>, ToolError>;
    fn pr_files(&self, pr: &str) -> Result<Vec<FileChange>, ToolError>;
    fn item_text(&self, item: &str) -> Result<String, ToolError>;
    fn open_findings(&self, item: &str, pr: &str) -> Result<Vec<String>, ToolError>;
    fn plan(&self, leg: &Leg, input: &PlanInput) -> Result<Vec<Vec<String>>, ToolError>;
    fn define_slice(&self, slice: &Slice) -> Result<(), ToolError>;
    fn close_pr_unmerged(&self, pr: &str) -> Result<(), ToolError>;
    /// Deliver `line`; `key` is the same on every retry of this job's report, so a receiver can
    /// drop a repeat. Return Ok only after the receiver or its durable spool took the line.
    fn send_routing(&self, key: &str, line: &str) -> Result<(), ToolError>;
}

/// The strongest available planning leg; the name decides a tie so the choice is stable.
pub(crate) fn strongest_leg(legs: &[Leg]) -> Option<&Leg> {
    legs.iter()
        .filter(|l| l.planning && l.available)
        .max_by(|a, b| {
            a.intelligence
                .total_cmp(&b.intelligence)
                .then_with(|| b.name.cmp(&a.name))
        })
}

fn generated(path: &str) -> bool {
    let name = path.rsplit('/').next().unwrap_or(path);
    name == "Cargo.lock"
        || name.ends_with(".lock")
        || name.contains(".generated.")
        || name.contains("_generated.")
        || path.split('/').any(|part| part == "generated")
}

/// Changed files that count toward the slice size.
pub(crate) fn counted(files: &[FileChange]) -> Vec<FileChange> {
    let mut kept: Vec<_> = files
        .iter()
        .filter(|f| !generated(&f.path))
        .cloned()
        .collect();
    kept.sort_by(|a, b| a.path.cmp(&b.path));
    kept
}

/// Greedy grouping by path order: a group closes before it would pass `target` lines; a file
/// above the target stands alone.
pub(crate) fn group_files(files: &[FileChange], target: u64) -> Vec<Vec<String>> {
    let (mut groups, mut current, mut lines) = (vec![], vec![], 0u64);
    for file in files {
        if !current.is_empty() && lines + file.lines > target {
            groups.push(std::mem::take(&mut current));
            lines = 0;
        }
        current.push(file.path.clone());
        lines += file.lines;
    }
    if !current.is_empty() {
        groups.push(current);
    }
    groups
}

pub(crate) fn job_dir(state: &Path) -> PathBuf {
    state.join("replans")
}
fn job_path(state: &Path, item: &str, pr: &str) -> PathBuf {
    let id = Sha256::digest(format!("{item}\n{pr}").as_bytes());
    job_dir(state).join(format!("replan-{:x}.json", id))
}
pub(crate) fn save(path: &Path, job: &Value) -> Result<(), ToolError> {
    atomic_write_private(path, job.to_string().as_bytes())
        .map_err(|_| parameter("replan_state_unavailable: job file not written"))
}
pub(crate) fn load(path: &Path) -> Result<Value, ToolError> {
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .ok_or_else(|| parameter("replan_state_invalid: job file unreadable"))
}

/// Queue the replan of `pr` of `item` after a refusal with `reason`. Returns true when this call
/// created the job; a repeated trigger finds the file and changes nothing.
pub(crate) fn enqueue(state: &Path, item: &str, pr: &str, reason: &str) -> Result<bool, ToolError> {
    if !TRIGGERS.contains(&reason) || pr.trim().is_empty() {
        return Ok(false);
    }
    std::fs::create_dir_all(job_dir(state))
        .map_err(|_| parameter("replan_state_unavailable: job directory missing"))?;
    let path = job_path(state, item, pr);
    if path.exists() {
        return Ok(false);
    }
    let job = json!({"item":item, "pr":pr, "reason":reason, "plan":null,
        "defined":[], "closed":false, "report":"pending", "error":null});
    save(&path, &job)?;
    Ok(true)
}

fn plan_step(job: &mut Value, ports: &dyn Ports) -> Result<(), ToolError> {
    let (item, pr) = (
        job["item"].as_str().unwrap_or("").to_string(),
        job["pr"].as_str().unwrap_or("").to_string(),
    );
    let legs = ports.legs()?;
    let leg = strongest_leg(&legs)
        .ok_or_else(|| parameter("replan_no_planning_leg: no available planning leg"))?;
    let files = counted(&ports.pr_files(&pr)?);
    let findings = ports.open_findings(&item, &pr)?;
    let text = ports.item_text(&item)?;
    let proposal = group_files(&files, SLICE_TARGET_LINES);
    let groups = ports.plan(
        leg,
        &PlanInput {
            item: &item,
            item_text: &text,
            files: &files,
            findings: &findings,
            target_lines: SLICE_TARGET_LINES,
            proposal: &proposal,
        },
    )?;
    let mut seen: Vec<&String> = groups.iter().flatten().collect();
    seen.sort();
    let mut want: Vec<&String> = files.iter().map(|f| &f.path).collect();
    want.sort();
    if groups.iter().any(|g| g.is_empty()) || seen != want {
        return Err(parameter(
            "replan_plan_invalid: slices must cover every counted file once",
        ));
    }
    let lines = |path: &String| {
        files
            .iter()
            .find(|f| &f.path == path)
            .map_or(0, |f| f.lines)
    };
    let slices: Vec<Value> = groups
        .iter()
        .enumerate()
        .map(|(n, group)| {
            json!({"key":format!("{item}-S{}", n + 1), "parent":item, "files":group,
                "lines":group.iter().map(lines).sum::<u64>(), "findings":findings})
        })
        .collect();
    job["plan"] = json!({"leg":leg.name, "slices":slices, "findings":findings});
    Ok(())
}

/// The idempotency key of the job's routing line: 32 lowercase hex characters, stable per item
/// and PR, so every retry of the same report carries the same key.
fn report_key(job: &Value) -> String {
    let level = job["level"]
        .as_u64()
        .map(|l| format!("\n{l}"))
        .unwrap_or_default();
    let id = Sha256::digest(format!(
        "replan-report\n{}\n{}{level}",
        job["item"].as_str().unwrap_or(""),
        job["pr"].as_str().unwrap_or("")
    ));
    format!("{id:x}")[..32].to_string()
}

fn slice_of(value: &Value) -> Slice {
    let strings = |v: &Value| {
        v.as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|s| s.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    };
    Slice {
        key: value["key"].as_str().unwrap_or("").into(),
        parent: value["parent"].as_str().unwrap_or("").into(),
        files: strings(&value["files"]),
        lines: value["lines"].as_u64().unwrap_or(0),
        findings: strings(&value["findings"]),
    }
}

fn drive(path: &Path, job: &mut Value, ports: &dyn Ports) -> Result<(), ToolError> {
    if job["plan"].is_null() {
        plan_step(job, ports)?;
        save(path, job)?;
    }
    let slices: Vec<Value> = job["plan"]["slices"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    // A job of a stall plans and reports only: the writer goes on, on the open PR.
    let keep_open = job["reason"] == crate::round_escalation_job::ESCALATED;
    for slice in slices.iter().filter(|_| !keep_open) {
        let key = slice["key"].clone();
        if job["defined"].as_array().is_some_and(|d| d.contains(&key)) {
            continue;
        }
        ports.define_slice(&slice_of(slice))?;
        job["defined"].as_array_mut().unwrap().push(key);
        save(path, job)?;
    }
    if job["closed"] != true && !keep_open {
        ports.close_pr_unmerged(job["pr"].as_str().unwrap_or(""))?;
        job["closed"] = json!(true);
        save(path, job)?;
    }
    if job["report"] == "pending" {
        let keys: Vec<_> = slices.iter().filter_map(|s| s["key"].as_str()).collect();
        let state = if keep_open {
            "kept open"
        } else {
            "closed unmerged"
        };
        let line = format!(
            "REPLAN {} {} | PR {} {state} | slices {}",
            job["item"].as_str().unwrap_or(""),
            job["reason"].as_str().unwrap_or(""),
            job["pr"].as_str().unwrap_or(""),
            keys.join(" ")
        );
        // Saved before the send and kept when the process dies in between: the next run sends
        // the same line under the same key and the receiver drops the repeat.
        job["report_line"] = json!(line);
        job["report"] = json!("sending");
        save(path, job)?;
    }
    if job["report"] == "sending" {
        let line = job["report_line"].as_str().unwrap_or("").to_string();
        ports.send_routing(&report_key(job), &line)?;
        // `sent` only after the delivery was acknowledged.
        job["report"] = json!("sent");
        save(path, job)?;
    }
    Ok(())
}

/// Drive one job to its end; on failure the step and the reason stay in the file for the next run.
fn run_job(path: &Path, ports: &dyn Ports) -> String {
    let message = |e: ToolError| e.into_error_data().message.to_string();
    let lock = match pg_worker_core::lockfile::try_lock(&path.with_extension("lock")) {
        Ok(Some(lock)) => lock,
        Ok(None) => return "busy".into(),
        Err(_) => return "replan_state_unavailable: job lock".into(),
    };
    let mut job = match load(path) {
        Ok(job) => job,
        Err(e) => return message(e),
    };
    if job["report"] == "sent" {
        return "done".into();
    }
    let outcome = drive(path, &mut job, ports);
    let text = outcome.err().map(message);
    job["error"] = text.as_deref().map_or(Value::Null, |t| json!(t));
    let saved = save(path, &job);
    drop(lock);
    match (text, saved) {
        (Some(text), _) => text,
        (None, Err(e)) => message(e),
        (None, Ok(())) => "done".into(),
    }
}

/// Run every queued job; the result lists `(job file, outcome)` with the refusal text on failure.
pub(crate) fn run_pending(state: &Path, ports: &dyn Ports) -> Vec<(String, String)> {
    let Ok(entries) = std::fs::read_dir(job_dir(state)) else {
        return vec![];
    };
    let mut paths: Vec<_> = entries
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    paths
        .iter()
        .map(|path| {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            (name, run_job(path, ports))
        })
        .collect()
}
