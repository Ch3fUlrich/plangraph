//! Private review-seat bridge; unknown or incomplete output never becomes ACCEPT.
use crate::evidence::{regular, EvidenceError};
use pg_worker_core::run::{record::run_dir, record_io::atomic_write_private};
use serde_json::{json, Value};
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
    let diagnostic =
        || Ok("Full rendered review unavailable or unsupported.\nVERDICT: HALT\n".into());
    if record["live_log"].as_str().map(Path::new) != Some(path.as_path()) {
        return diagnostic();
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
        Err(_) => return diagnostic(),
    };
    if !file.metadata()?.is_file() || file.metadata()?.len() > 8 * 1024 * 1024 {
        return diagnostic();
    }
    let mut text = String::new();
    file.take(8 * 1024 * 1024 + 1).read_to_string(&mut text)?;
    if text.len() > 8 * 1024 * 1024 || !text.ends_with('\n') {
        return diagnostic();
    }
    let mut result = None;
    for line in text.lines() {
        let Ok(frame) = serde_json::from_str::<Value>(line) else {
            return diagnostic();
        };
        if frame["type"] == "result" && frame["is_error"] == false {
            result = frame["result"].as_str().map(str::to_owned);
        }
    }
    match result {
        Some(text) if !text.is_empty() => Ok(text),
        _ => diagnostic(),
    }
}
pub(crate) fn write_review_artifacts(
    state: &Path,
    record: &Value,
    review: &str,
) -> Result<(), EvidenceError> {
    let dir = directory(state, record)?;
    let seat = dir.join("seat");
    regular(&seat, true)?;
    if crate::record_status::prompt_only_missing(record, &dir) {
        for name in ["evidence.json", "review.txt"] {
            match fs::remove_file(seat.join(name)) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) => return Err(error.into()),
            }
        }
        return Err(EvidenceError::Invalid);
    }
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
            pg_worker_core::redact::redact(review).into_bytes(),
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
