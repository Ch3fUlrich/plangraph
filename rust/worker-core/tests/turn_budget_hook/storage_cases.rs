use super::*;

fn invoke(f: &Fixture, payload: Value) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_turn-budget-hook"))
        .env_clear()
        .env("FLEET_STATE_DIR", &f.config.state_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.to_string().as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn status_io_failure_is_reported_with_os_error_and_nonzero_exit() {
    let f = Fixture::new();
    fs::write(&f.config.state_dir, "not a directory").unwrap();
    let expected = fs::create_dir_all(f.config.state_dir.join("status"))
        .unwrap_err()
        .to_string();
    let mut errors = Vec::new();
    let payload = json!({"session_id":"test-session", "hook_event_name":"Stop"});
    assert_eq!(
        pg_worker_core::turn_budget::handle_with_diagnostics(&payload, &f.config, START, |e| {
            errors.push(e.to_string())
        }),
        None
    );
    assert_eq!(errors, std::slice::from_ref(&expected));
    for command in ["echo work", "sleep 2; git status"] {
        let payload = json!({"session_id":"test-session", "hook_event_name":"PreToolUse",
            "tool_name":"Bash", "tool_input":{"command":command}});
        let out = invoke(&f, payload);
        let stderr = String::from_utf8(out.stderr).unwrap();
        assert!(stderr.contains(&expected), "{stderr:?}");
        assert_eq!(
            out.status.code(),
            Some(if command == "echo work" { 1 } else { 2 })
        );
        assert!(out.stdout.is_empty());
    }
}

#[test]
fn failed_atomic_write_is_reported_even_when_history_refuses_the_tool() {
    let f = Fixture::new();
    let payload = json!({"session_id":"test-session", "hook_event_name":"PreToolUse",
        "tool_name":"Bash", "tool_input":{"command":"cat status.json"}});
    assert_eq!(handle(&payload, &f.config, START), None);
    let path = f.config.state_dir.join("status/test-session.json");
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    document["other"] = json!("x".repeat(1024 * 1024 - 1024));
    let original = document.to_string();
    assert!(original.len() < 1024 * 1024);
    fs::write(&path, &original).unwrap();
    for i in 0..100 {
        f.call(&format!("message-{i}"), 1);
    }
    for event in ["Stop", "PreToolUse"] {
        let mut payload = payload.clone();
        payload["hook_event_name"] = json!(event);
        payload["transcript_path"] = json!(f.dir.path().join("transcript.jsonl"));
        let out = invoke(&f, payload);
        assert_eq!(out.status.code(), Some(if event == "Stop" { 1 } else { 2 }));
        assert!(String::from_utf8(out.stderr)
            .unwrap()
            .contains("status too large"));
        assert!(out.stdout.is_empty());
        assert_eq!(fs::read(&path).unwrap(), original.as_bytes());
    }
}

fn entries(dir: &std::path::Path) -> Vec<std::ffi::OsString> {
    let mut entries: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    entries.sort();
    entries
}

#[test]
fn over_cap_status_is_preserved_and_repeated_reads_exit_allow() {
    let f = Fixture::new();
    let dir = f.config.state_dir.join("status");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("test-session.json");
    let original = json!({"other":"x".repeat(1024 * 1024)}).to_string();
    fs::write(&path, &original).unwrap();
    assert!(fs::metadata(&path).unwrap().len() > 1024 * 1024);
    for _ in 0..3 {
        let payload = json!({"session_id":"test-session", "hook_event_name":"PreToolUse",
            "tool_name":"Bash", "tool_input":{"command":"cat status.json"}});
        let mut child = Command::new(env!("CARGO_BIN_EXE_turn-budget-hook"))
            .env_clear()
            .env("HOME", f.dir.path())
            .env("FLEET_STATE_DIR", &f.config.state_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(payload.to_string().as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stdout.is_empty() && output.stderr.is_empty());
        assert_eq!(fs::read(&path).unwrap(), original.as_bytes());
        assert_eq!(entries(&dir), ["test-session.json", "test-session.lock"]);
    }
}

#[test]
fn rejected_over_cap_update_preserves_original_bytes_and_exact_directory_entries() {
    let f = Fixture::new();
    f.event("Stop", START);
    let dir = f.config.state_dir.join("status");
    let path = dir.join("test-session.json");
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    document["other"] = json!("x".repeat(1024 * 1024 - 1024));
    let original = document.to_string();
    assert!(original.len() < 1024 * 1024);
    fs::write(&path, &original).unwrap();
    let before = entries(&dir);
    for i in 0..100 {
        f.call(&format!("message-{i}"), 1);
    }
    f.event("Stop", START + 1);
    assert_eq!(fs::read(&path).unwrap(), original.as_bytes());
    assert_eq!(entries(&dir), before);
    assert_eq!(before, ["test-session.json", "test-session.lock"]);
}

#[test]
fn concurrent_reader_only_observes_complete_atomic_status_documents() {
    use std::io::{Read, Seek};
    use std::sync::mpsc;
    let f = Fixture::new();
    f.event("Stop", START);
    let path = f.config.state_dir.join("status/test-session.json");
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    document["other"] = json!("x".repeat(128 * 1024));
    fs::write(&path, document.to_string()).unwrap();
    std::thread::scope(|scope| {
        // Failure watchdog only: allow a loaded runner ample time per write,
        // without measuring production latency or retrying a failed read.
        let (ready_tx, ready_rx) = mpsc::channel();
        let (written_tx, written_rx) = mpsc::channel();
        let path = &path;
        let reader = scope.spawn(move || {
            let mut reads = 0;
            for index in 1..=30 {
                // Keep the old document open throughout replacement, rather
                // than hoping the scheduler overlaps a short fs::read handle.
                let mut held = fs::File::open(path).unwrap();
                let mut original = Vec::new();
                held.read_to_end(&mut original).unwrap();
                let old: Value = serde_json::from_slice(&original).unwrap();
                assert_eq!(old["turn_budget"]["wake_calls"], index - 1);
                ready_tx.send(()).unwrap();
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
                loop {
                    match written_rx.try_recv() {
                        Ok(()) => break,
                        Err(mpsc::TryRecvError::Disconnected) => panic!("writer disconnected"),
                        Err(mpsc::TryRecvError::Empty) => (),
                    }
                    assert!(std::time::Instant::now() < deadline, "writer timed out");
                    // Each concurrent observation must parse; neither missing
                    // nor partial documents are retried or ignored.
                    let raw = fs::read(path).unwrap();
                    let value: Value = serde_json::from_slice(&raw).unwrap();
                    assert_eq!(value["other"].as_str().unwrap().len(), 128 * 1024);
                    assert!(value["turn_budget"].is_object());
                    let count = value["turn_budget"]["wake_calls"].as_u64().unwrap();
                    assert!((index - 1..=index).contains(&count));
                    reads += 1;
                }
                held.rewind().unwrap();
                let mut retained = Vec::new();
                held.read_to_end(&mut retained).unwrap();
                // This also rejects truncate/write or remove/write substitutes:
                // a held snapshot must never change, even briefly, in place.
                assert!(
                    retained == original,
                    "replacement modified the held snapshot"
                );
                for _ in 0..4 {
                    let raw = fs::read(path).unwrap();
                    let value: Value = serde_json::from_slice(&raw).unwrap();
                    assert_eq!(value["other"].as_str().unwrap().len(), 128 * 1024);
                    assert!(value["turn_budget"].is_object());
                    assert_eq!(value["turn_budget"]["wake_calls"], index);
                    reads += 1;
                }
            }
            reads
        });
        for index in 1..=30 {
            ready_rx
                .recv_timeout(std::time::Duration::from_secs(30))
                .unwrap();
            f.call(&format!("atomic-{index}"), index);
            f.event("Stop", START + index);
            written_tx.send(()).unwrap();
        }
        assert!(reader.join().unwrap() >= 100);
    });
    assert_eq!(f.status()["wake_calls"], 30);
    assert_eq!(
        entries(path.parent().unwrap()),
        ["test-session.json", "test-session.lock"]
    );
}
