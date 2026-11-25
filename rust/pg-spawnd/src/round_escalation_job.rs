//! The replan job of a stall: the saved record that governs the admission of the stalled round
//! (`round_escalate::govern`). The job plans the slices of the PR and reports them (the
//! `round_replan` runner); the PR stays open, nothing is defined or closed, and the writer
//! resumes with the slices in its prompt.
use crate::errors::ToolError;
use crate::placement::parameter;
use crate::round_replan::{job_dir, load, save};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

/// The reason of a replan queued by a stall.
pub(crate) const ESCALATED: &str = "escalated";

fn path(state: &Path, batch: &str, pr: &str, level: u64) -> PathBuf {
    let id = Sha256::digest(format!("{batch}\n{pr}\n{ESCALATED}\n{level}").as_bytes());
    job_dir(state).join(format!("replan-{id:x}.json"))
}

/// Queue the replan job of the stall to `level`; false when it already exists.
pub(crate) fn enqueue(state: &Path, batch: &str, pr: &str, level: u64) -> Result<bool, ToolError> {
    std::fs::create_dir_all(job_dir(state))
        .map_err(|_| parameter("replan_state_unavailable: job directory missing"))?;
    let path = path(state, batch, pr, level);
    if path.exists() {
        return Ok(false);
    }
    let job = json!({"item":batch, "pr":pr, "reason":ESCALATED, "level":level,
        "plan":null, "defined":[], "closed":false, "report":"pending", "error":null});
    save(&path, &job)?;
    Ok(true)
}

/// The state of the job of `level`: None when it was never queued, Some(true) once it planned its
/// slices and delivered its report, Some(false) while it is still on its way.
pub(crate) fn sent(state: &Path, batch: &str, pr: &str, level: u64) -> Option<bool> {
    let job = load(&path(state, batch, pr, level)).ok()?;
    Some(job["report"] == "sent" && !job["plan"].is_null())
}

/// The delivered replan as a prompt addendum for the resumed writer: the slices with their files
/// and the open findings, verbatim (kept at plan level, so a diff with no counted file keeps them).
pub(crate) fn brief(state: &Path, batch: &str, pr: &str, level: u64) -> Option<String> {
    let job = load(&path(state, batch, pr, level)).ok()?;
    if job["report"] != "sent" || job["plan"].is_null() {
        return None;
    }
    let mut text = format!(
        "REPLAN of {batch} after the stall (level {level}): continue on this PR and keep to these slices.\n"
    );
    for slice in job["plan"]["slices"].as_array().into_iter().flatten() {
        let files: Vec<&str> = slice["files"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        text += &format!(
            "- {}: {}\n",
            slice["key"].as_str().unwrap_or(""),
            files.join(", ")
        );
    }
    let findings = job["plan"]["findings"]
        .as_array()
        .or_else(|| job["plan"]["slices"][0]["findings"].as_array());
    for finding in findings.into_iter().flatten().filter_map(Value::as_str) {
        text += &format!("Open finding: {finding}\n");
    }
    Some(text)
}
