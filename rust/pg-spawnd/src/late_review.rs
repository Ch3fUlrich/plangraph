//! The native review run can finish after its caller's wait ends.
use pg_worker_core::run::{finalize::now, record::valid_run_id, record_io::atomic_write_private};
use serde_json::{json, Value};
use std::{
    fs,
    path::Path,
    process::{Command, Stdio},
    time::Duration,
};

pub(crate) fn live(record: &Value) -> bool {
    matches!(record["status"].as_str(), Some("queued" | "running"))
}

pub(crate) fn save_pending(path: &Path, request: &Value) -> Result<(), String> {
    atomic_write_private(path, request.to_string().as_bytes()).map_err(|e| e.to_string())
}

pub(crate) fn read_pending(path: &Path) -> Result<Option<Value>, String> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|e| e.to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

pub(crate) fn read_run(state: &Path, request: &Value) -> Result<Value, String> {
    crate::records::read_record(state, &request["run_id"])
        .map_err(|e| e.into_error_data().message.into_owned())
}

pub(crate) fn pending_result(request: &Value) -> Value {
    json!({
        "status":"review_running","head":request["head"],"run_id":request["run_id"],
        "expected_end":request["expected_end"],
        "reason":format!("review_running: {} expected end {} (Unix time)",
            request["run_id"], request["expected_end"])
    })
}

pub(crate) fn publish(
    state: &Path,
    pending: &Path,
    request: &Value,
    record: &Value,
) -> Result<Option<Value>, String> {
    if request["run_id"] != record["run_id"] {
        return Err("late review run id mismatch".into());
    }
    if live(record) {
        return Ok(None);
    }
    let head = request["head"].as_str().ok_or("late review head missing")?;
    let paths = serde_json::from_value::<Vec<String>>(request["changed_paths"].clone())
        .map_err(|e| e.to_string())?;
    let text = crate::native_writer_host::review_run_text(state, record)?;
    let mut result = crate::native_writer_host::review_verdict(&text, head, &paths);
    let file = pending.with_extension("txt");
    let artifact = format!("HEAD: {head}\n{text}");
    atomic_write_private(&file, artifact.as_bytes()).map_err(|e| e.to_string())?;
    result["path"] = json!(file);
    result["run_id"] = record["run_id"].clone();
    result["leg"] = record["chosen_leg"]["id"].clone();
    if let Some(checkout) = request["checkout"].as_str().map(Path::new).filter(|p| {
        p.parent() == pending.parent()
            && p.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|n| n.starts_with("checkout-"))
    }) {
        let _ = fs::remove_dir_all(checkout);
    }
    Ok(Some(result))
}

/// Start a local finisher with no inherited pipes, so the review CLI can return the receipt.
pub(crate) fn start(state: &Path, pending: &Path) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let mut command = Command::new(exe);
    command
        .arg("finish-late-review")
        .arg(state)
        .arg(pending)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command.spawn().map_err(|e| e.to_string())?;
    Ok(())
}

pub(crate) fn execute(args: Vec<String>) -> Result<(), String> {
    let [state, pending] = args.as_slice() else {
        return Err("finish-late-review needs a state path and pending receipt".into());
    };
    let (state, pending) = (Path::new(state), Path::new(pending));
    let Some(request) = read_pending(pending)? else {
        return Ok(());
    };
    if !request["run_id"].as_str().is_some_and(valid_run_id) {
        return Err("invalid late review run id".into());
    }
    let expected_end = request["expected_end"]
        .as_f64()
        .ok_or("late review deadline missing")?;
    // Allow a final record save and remote status propagation after the card deadline.
    while now() < expected_end + 600.0 {
        if let Ok(record) = read_run(state, &request) {
            if publish(state, pending, &request, &record)?.is_some() {
                return Ok(());
            }
        }
        std::thread::sleep(Duration::from_secs(2));
    }
    Err("late review did not reach terminal status".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn delayed_completion_writes_the_verdict_bound_to_its_head() {
        let dir = tempfile::tempdir().unwrap();
        let pending = dir.path().join("review-abc.pending.json");
        let request = json!({"head":"abc","run_id":"run-fixture","expected_end":3600,
            "changed_paths":["src/lib.rs"]});
        save_pending(&pending, &request).unwrap();
        let mut record = json!({"run_id":"run-fixture","status":"running","started":0,
            "stdout_tail":"src/lib.rs checked\nVERDICT: ACCEPT"});
        assert!(publish(dir.path(), &pending, &request, &record)
            .unwrap()
            .is_none());
        assert!(!pending.with_extension("txt").exists());
        record["status"] = json!("completed");
        let result = publish(dir.path(), &pending, &request, &record)
            .unwrap()
            .unwrap();
        assert_eq!(result["head"], "abc");
        assert_eq!(result["verdict"], "ACCEPT");
        let text = std::fs::read_to_string(result["path"].as_str().unwrap()).unwrap();
        assert!(text.contains("HEAD: abc\n"));
        assert!(text.contains("VERDICT: ACCEPT"));
    }
}
