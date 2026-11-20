//! Private review-seat bridge; unknown or incomplete output never becomes ACCEPT.
use crate::evidence::{regular, EvidenceError};
use pg_worker_core::run::{record::run_dir, record_io::atomic_write_private};
use serde_json::{json, Value};

/// Text of the review artifact when no rendered review exists; `seat_verdict` recognises it.
pub(crate) const DIAGNOSTIC_REVIEW_TEXT: &str =
    "Full rendered review unavailable or unsupported.\nVERDICT: HALT\n";
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
};
fn directory(state: &Path, record: &Value) -> Result<PathBuf, EvidenceError> {
    let dir = run_dir(
        state,
        record["run_id"].as_str().ok_or(EvidenceError::Invalid)?,
    )
    .map_err(|_| EvidenceError::Invalid)?;
    regular(&state.join("runs"), true)?;
    regular(&dir, true)?;
    Ok(dir)
}
pub(crate) fn review_text(state: &Path, record: &Value) -> Result<String, EvidenceError> {
    let dir = directory(state, record)?;
    let path = dir.join("live.log");
    let agy = record["attempts"]
        .as_array()
        .and_then(|a| a.last())
        .is_some_and(|a| a["leg"]["harness"] == "agy" && crate::cli_review::eligible(&a["leg"]))
        && record["status"] != "dry_run";
    let diagnostic = |details: &str| {
        if agy {
            let detail: String = details
                .chars()
                .rev()
                .take(8192)
                .collect::<String>()
                .chars()
                .rev()
                .collect();
            let stderr: String = record["stderr_tail"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(8192)
                .collect();
            Ok(format!("Agy review unavailable: empty, failed or unsupported output.\nDiagnostics (untrusted): {}\nVERDICT: FAIL\nTEST_GAMING: yes\n", json!({"log":detail, "stderr":stderr})))
        } else {
            Ok(DIAGNOSTIC_REVIEW_TEXT.into())
        }
    };
    if record["live_log"].as_str().map(Path::new) != Some(path.as_path()) {
        return diagnostic("");
    }
    regular(&path, false)?;
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = match options.open(path) {
        Ok(f) => f,
        Err(_) => return diagnostic(""),
    };
    if !file.metadata()?.is_file() || file.metadata()?.len() > 8 * 1024 * 1024 {
        return diagnostic("");
    }
    let mut text = String::new();
    file.take(8 * 1024 * 1024 + 1).read_to_string(&mut text)?;
    if text.len() > 8 * 1024 * 1024 || !text.ends_with('\n') {
        return diagnostic(&text);
    }
    match pg_worker_core::review_split::extract_review_text_from_live_log(&text) {
        Some(result) if !result.is_empty() && (!agy || !result.trim().is_empty()) => Ok(result),
        _ => diagnostic(&text),
    }
}
#[cfg(test)]
pub(crate) fn write_review_artifacts(
    state: &Path,
    record: &Value,
    review: &str,
) -> Result<(), EvidenceError> {
    write_review_artifacts_with_allowlist(state, record, review, &Default::default())
}
pub(crate) fn write_review_artifacts_with_allowlist(
    state: &Path,
    record: &Value,
    review: &str,
    allow_shas: &std::collections::BTreeSet<String>,
) -> Result<(), EvidenceError> {
    let seat = directory(state, record)?.join("seat");
    regular(&seat, true)?;
    let mut evidence = json!({});
    for key in [
        "served_model",
        "requested_model",
        "finish_reason",
        "verdict",
        "test_gaming",
        "correlation_id",
        "prompt_sha256",
        "witness_kind",
        "request_side_model",
    ] {
        evidence[key] = record[key].clone();
    }
    // Never promote a requested model to a served-model witness.
    if record["served_model"].is_null() && record["witness_kind"].is_null() {
        evidence["served_model"] = json!("unwitnessed");
        evidence["requested_model"] = Value::Null;
    }
    let known = |key: &str| {
        record[key].as_str().is_some_and(|s| {
            !s.trim().is_empty()
                && !["unsure", "unknown", "unwitnessed", "null"]
                    .contains(&s.to_lowercase().as_str())
        })
    };
    let passing = matches!(record["finish_reason"].as_str(), Some("stop" | "end_turn"))
        && known("served_model")
        && known("witness_kind")
        && (record["test_gaming"] == false || record["test_gaming"] == "no");
    // Only exact canonical lines can authorize PASS; a later verdict-like line
    // invalidates an earlier decision rather than falling back to it.
    let accepts = review
        .lines()
        .rev()
        .find(|line| line.to_ascii_lowercase().contains("verdict"))
        == Some("VERDICT: PASS");
    if !passing || !accepts {
        evidence["verdict"] = json!("HALT");
    }
    for (name, bytes) in [
        (
            "review.txt",
            pg_worker_core::redact::redact_with_allowlist(review, allow_shas).into_bytes(),
        ),
        (
            "evidence.json",
            pg_worker_core::redact::redact(&evidence.to_string()).into_bytes(),
        ),
    ] {
        let path = seat.join(name);
        regular(&path, false)?;
        atomic_write_private(&path, &bytes)?;
    }
    Ok(())
}
#[cfg(test)]
#[path = "review_artifacts_tests.rs"]
mod tests;
