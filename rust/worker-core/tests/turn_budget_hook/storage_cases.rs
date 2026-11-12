use super::*;

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
