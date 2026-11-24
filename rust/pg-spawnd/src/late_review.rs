//! The native review run can finish after its caller's wait ends.
use pg_worker_core::{
    review_completion::{self, GRACE_SECONDS},
    run::{
        finalize::now,
        record::valid_run_id,
        record_io::{atomic_write_private, ensure_private_dir},
    },
};
use serde_json::{json, Value};
use std::{fs, path::Path, time::Duration};

pub(crate) fn live(record: &Value) -> bool {
    record["status"]
        .as_str()
        .is_some_and(review_completion::live)
}

pub(crate) fn save_pending(path: &Path, request: &Value) -> Result<(), String> {
    atomic_write_private(path, request.to_string().as_bytes()).map_err(|e| e.to_string())
}

fn read_pending(path: &Path) -> Result<Option<Value>, String> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|e| e.to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn receipt_lock(pending: &Path) -> Result<fs::File, String> {
    let dir = pending.parent().ok_or("late review directory missing")?;
    ensure_private_dir(dir).map_err(|e| e.to_string())?;
    let lock = fs::File::options()
        .create(true)
        .truncate(false)
        .write(true)
        .open(pending.with_extension("lock"))
        .map_err(|e| e.to_string())?;
    lock.lock().map_err(|e| e.to_string())?;
    Ok(lock)
}

fn remove(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

pub(crate) fn recover(
    state: &Path,
    pending: &Path,
    head: &str,
    binding: &Value,
) -> Result<Option<Value>, String> {
    let _lock = receipt_lock(pending)?;
    let ready = pending.with_extension("ready.json");
    if let Some(result) = read_pending(&ready)? {
        remove(&ready)?;
        if result["head"] == head && result["test_replay_binding"] == *binding {
            remove(pending)?;
            return Ok(Some(result));
        }
    }
    let Some(request) = read_pending(pending)? else {
        return Ok(None);
    };
    if request["head"] != head || request["test_replay_binding"] != *binding {
        discard(pending, &request)?;
        return Ok(None);
    }
    let record = match read_run(state, &request) {
        Ok(record) => record,
        Err(_) => {
            discard(pending, &request)?;
            return Ok(None);
        }
    };
    if live(&record) {
        return Ok(Some(pending_result(&request)));
    }
    finish_pending(state, pending, &request, &record, None)
}

fn read_run(state: &Path, request: &Value) -> Result<Value, String> {
    let record = crate::records::read_record(state, &request["run_id"])
        .map_err(|e| e.into_error_data().message.into_owned())?;
    if record["run_id"] != request["run_id"] {
        return Err("late review run id mismatch".into());
    }
    Ok(record)
}

pub(crate) fn pending_result(request: &Value) -> Value {
    json!({
        "status":"review_running","head":request["head"],"run_id":request["run_id"],
        "expected_end":request["expected_end"], "test_replay_binding":request["test_replay_binding"],
        "reason":format!("review_running: {} expected end {} (Unix time)",
            request["run_id"], request["expected_end"])
    })
}

fn publish(
    state: &Path,
    pending: &Path,
    request: &Value,
    record: &Value,
) -> Result<Option<Value>, String> {
    if request["run_id"] != record["run_id"] {
        return Err("late review run id mismatch".into());
    }
    if record["status"] != "completed" {
        return Ok(None);
    }
    let head = request["head"].as_str().ok_or("late review head missing")?;
    let paths = serde_json::from_value::<Vec<String>>(request["changed_paths"].clone())
        .map_err(|e| e.to_string())?;
    let mut result = crate::native_writer_host::review_record_verdict(
        state,
        record,
        head,
        &paths,
        request["expected_leg"].as_str(),
    )?;
    let text = result["text"].as_str().unwrap_or_default();
    let file = pending.with_extension("txt");
    let artifact = if result["verdict"] == "no_verdict" {
        format!("{}\n\nVERDICT: NO_VERDICT\n", text.trim_end())
    } else {
        format!("HEAD: {head}\n{text}")
    };
    atomic_write_private(&file, artifact.as_bytes()).map_err(|e| e.to_string())?;
    result["path"] = json!(file);
    result["test_replay_binding"] = request["test_replay_binding"].clone();
    Ok(Some(result))
}

fn finish_pending(
    state: &Path,
    pending: &Path,
    request: &Value,
    record: &Value,
    ready: Option<&Path>,
) -> Result<Option<Value>, String> {
    // Unusable evidence must not prevent a fresh run for this head.
    let result = publish(state, pending, request, record).unwrap_or(None);
    if let (Some(result), Some(ready)) = (&result, ready) {
        save_pending(ready, result)?;
    }
    discard(pending, request)?;
    Ok(result)
}

fn discard(pending: &Path, request: &Value) -> Result<(), String> {
    remove(pending)?;
    clean_checkout(pending, request);
    Ok(())
}

fn clean_checkout(pending: &Path, request: &Value) {
    if let Some(checkout) = request["checkout"].as_str().map(Path::new).filter(|p| {
        p.parent() == pending.parent()
            && p.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|n| n.starts_with("checkout-"))
    }) {
        let _ = fs::remove_dir_all(checkout);
    }
}

/// Start a local finisher with no inherited pipes, so the review CLI can return the receipt.
pub(crate) fn start(state: &Path, pending: &Path) -> Result<(), String> {
    review_completion::start([
        "finish-late-review".as_ref(),
        state.as_os_str(),
        pending.as_os_str(),
    ])
    .map_err(|e| e.to_string())
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
        .filter(|n| n.is_finite())
        .ok_or("late review deadline missing")?;
    loop {
        {
            let _lock = receipt_lock(pending)?;
            if read_pending(pending)?.as_ref() != Some(&request) {
                return Ok(());
            }
            let record = match read_run(state, &request) {
                Ok(record) => record,
                Err(error) => {
                    discard(pending, &request)?;
                    return Err(error);
                }
            };
            if !live(&record) {
                let ready = pending.with_extension("ready.json");
                return finish_pending(state, pending, &request, &record, Some(&ready)).map(|_| ());
            }
            // Allow the run record to settle after the card deadline.
            if now() >= expected_end + GRACE_SECONDS {
                remove(pending)?;
                return Err("late review did not reach terminal status".into());
            }
        }
        std::thread::sleep(Duration::from_secs(2));
    }
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
        let mut record = json!({"run_id":"run-fixture","head_sha":"abc","host":"fixture-worker","status":"running","started":0,
            "stdout_tail":"FILES READ: src/lib.rs\nVERDICT: ACCEPT"});
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
    #[test]
    fn finisher_retires_pending_receipt_and_keeps_completion_recoverable() {
        let dir = tempfile::tempdir().unwrap();
        let pending = dir.path().join("review-receipt.json");
        let request = json!({"head":"abc","run_id":"run-fixture","expected_end":now()+10.0,
            "changed_paths":["src/lib.rs"]});
        save_pending(&pending, &request).unwrap();
        let run = dir.path().join("runs/run-fixture/record.json");
        save_pending(
            &run,
            &json!({"run_id":"run-fixture","head_sha":"abc","host":"fixture-worker","status":"completed",
            "stdout_tail":"FILES READ: src/lib.rs\nVERDICT: ACCEPT"}),
        )
        .unwrap();
        execute(vec![
            dir.path().display().to_string(),
            pending.display().to_string(),
        ])
        .unwrap();
        assert!(pending.with_extension("txt").is_file());
        assert!(!pending.exists());
        fs::remove_file(run).unwrap();
        let result = recover(dir.path(), &pending, "abc", &Value::Null)
            .unwrap()
            .unwrap();
        assert_eq!(result["run_id"], "run-fixture");
        assert_eq!(result["verdict"], "ACCEPT");
        assert!(recover(dir.path(), &pending, "abc", &Value::Null)
            .unwrap()
            .is_none());
    }
    #[test]
    fn failed_run_with_accept_text_retires_without_a_verdict() {
        check_retired("failed", json!(["src/lib.rs"]));
    }

    #[test]
    fn unreadable_review_retires_and_allows_retry() {
        check_retired("completed", json!("not a path list"));
    }

    fn check_retired(status: &str, paths: Value) {
        for background in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let pending = dir.path().join("review-receipt.json");
            let checkout = dir.path().join("checkout-fixture");
            fs::create_dir(&checkout).unwrap();
            save_pending(
                &pending,
                &json!({"head":"abc","run_id":"run-fixture",
                "expected_end":now()+10.0,"changed_paths":paths,"checkout":checkout}),
            )
            .unwrap();
            save_pending(
                &dir.path().join("runs/run-fixture/record.json"),
                &json!({"run_id":"run-fixture","head_sha":"abc","host":"fixture-worker","status":status,
                    "stdout_tail":"FILES READ: src/lib.rs\nVERDICT: ACCEPT"}),
            )
            .unwrap();
            if background {
                execute(vec![
                    dir.path().display().to_string(),
                    pending.display().to_string(),
                ])
                .unwrap();
            } else {
                assert!(recover(dir.path(), &pending, "abc", &Value::Null)
                    .unwrap()
                    .is_none());
            }
            assert!(!pending.exists());
            assert!(!checkout.exists());
            assert!(!pending.with_extension("txt").exists());
            assert!(!pending.with_extension("ready.json").exists());
            assert!(recover(dir.path(), &pending, "abc", &Value::Null)
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn weak_late_review_artifact_ends_with_the_validated_result() {
        let dir = tempfile::tempdir().unwrap();
        let pending = dir.path().join("review-receipt.json");
        let request = json!({"head":"abc","run_id":"run-fixture","changed_paths":["src/lib.rs"]});
        let record = json!({"run_id":"run-fixture","head_sha":"abc","host":"fixture-worker","status":"completed","stdout_tail":"VERDICT: ACCEPT"});
        let result = publish(dir.path(), &pending, &request, &record)
            .unwrap()
            .unwrap();
        assert_eq!(result["verdict"], "no_verdict");
        let text = fs::read_to_string(pending.with_extension("txt")).unwrap();
        assert!(text.ends_with("VERDICT: NO_VERDICT\n"), "{text}");
        assert!(!text.starts_with("HEAD: abc\n"), "{text}");
    }
}
