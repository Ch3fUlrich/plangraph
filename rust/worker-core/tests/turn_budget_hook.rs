use pg_worker_core::turn_budget::{handle, Config};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    process::{Command, Stdio},
};
#[path = "turn_budget_hook/storage_cases.rs"]
mod storage_cases;

struct Fixture {
    dir: tempfile::TempDir,
    config: Config,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let config = Config {
            state_dir: dir.path().join("state"),
            launch_dir: dir.path().join("launch"),
            session_name: Some("build-L1-0429".into()),
        };
        Self { dir, config }
    }
    fn event(&self, event: &str, now: u64) {
        let payload = json!({"session_id":"test-session", "hook_event_name":event,
            "transcript_path":self.dir.path().join("transcript.jsonl")});
        assert_eq!(handle(&payload, &self.config, now), None);
    }
    fn append(&self, value: Value) {
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.dir.path().join("transcript.jsonl"))
            .unwrap();
        writeln!(file, "{value}").unwrap();
    }
    fn call(&self, id: &str, seconds: u64) {
        self.append(json!({"type":"assistant", "timestamp":chrono::DateTime::from_timestamp((START + seconds) as i64, 0).unwrap().to_rfc3339(),
            "message":{"id":id,"role":"assistant","content":[]}}));
    }
    fn status(&self) -> Value {
        serde_json::from_slice::<Value>(
            &fs::read(self.config.state_dir.join("status/test-session.json")).unwrap(),
        )
        .unwrap()["turn_budget"]
            .clone()
    }
}
const START: u64 = 1767225600;

#[test]
fn counts_model_messages_not_tools_and_resets_only_on_wake() {
    let f = Fixture::new();
    f.event("UserPromptSubmit", START);
    f.call("message-1", 1);
    f.call("message-1", 1); // multiple content blocks from one API response
    f.event("PreToolUse", START + 1);
    f.event("PostToolUse", START + 1);
    f.call("message-2", 2);
    f.event("Stop", START + 2);
    assert_eq!(f.status()["wake_calls"], 2);
    assert_eq!(f.status()["hour_calls"], 2);
    assert_eq!(f.status()["role"], "build-L1");
    f.event("Notification", START + 3);
    assert_eq!(f.status()["wake_calls"], 0);
    f.call("message-3", 4);
    f.event("Stop", START + 4);
    assert_eq!(f.status()["wake_calls"], 1);
    assert_eq!(f.status()["hour_calls"], 3);
    f.event("UserPromptSubmit", START + 5);
    assert_eq!(f.status()["wake_calls"], 0);
}

#[test]
fn rolling_hour_expires_at_boundary_but_keeps_wake() {
    let f = Fixture::new();
    f.call("first", 1);
    f.event("PostToolUse", START + 1);
    f.call("second", 30);
    f.event("Stop", START + 30);
    f.event("PostToolUse", START + 3601);
    assert_eq!(f.status()["hour_calls"], 1);
    assert_eq!(f.status()["wake_calls"], 2);
    f.event("PostToolUse", START + 3630);
    assert_eq!(f.status()["hour_calls"], 0);
}

#[test]
fn tool_results_and_meta_records_are_not_model_calls() {
    let f = Fixture::new();
    f.append(json!({"type":"user","message":{"content":[{"type":"tool_result"}]}}));
    f.append(json!({"type":"progress","message":{"id":"progress"}}));
    f.append(json!({"type":"assistant","message":{"content":[]}}));
    f.event("Stop", START);
    assert_eq!(f.status()["wake_calls"], 0);
}

#[test]
fn partial_transcript_retries_and_truncation_does_not_double_count() {
    let f = Fixture::new();
    let path = f.dir.path().join("transcript.jsonl");
    fs::write(
        &path,
        "{\"type\":\"assistant\",\"message\":{\"id\":\"one\"}}",
    )
    .unwrap();
    f.event("PreToolUse", START);
    assert_eq!(f.status()["wake_calls"], 0);
    fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap()
        .write_all(b"\n")
        .unwrap();
    f.event("PostToolUse", START);
    assert_eq!(f.status()["wake_calls"], 1);
    fs::write(&path, "").unwrap();
    f.event("PostToolUse", START);
    f.call("one", 1);
    f.call("two", 2);
    f.event("Stop", START + 2);
    assert_eq!(f.status()["wake_calls"], 2);
}

#[test]
fn preserves_other_status_fields_and_resolves_launch_record() {
    let mut f = Fixture::new();
    f.config.session_name = None;
    fs::create_dir_all(&f.config.launch_dir).unwrap();
    fs::write(
        f.config.launch_dir.join("launch.json"),
        json!({"session_id":"test-session","name":"review-L2-0021"}).to_string(),
    )
    .unwrap();
    let status_dir = f.config.state_dir.join("status");
    fs::create_dir_all(&status_dir).unwrap();
    fs::write(
        status_dir.join("test-session.json"),
        r#"{"other":"untouched"}"#,
    )
    .unwrap();
    f.event("UserPromptSubmit", START);
    assert_eq!(f.status()["role"], "review-L2");
    let raw: Value =
        serde_json::from_slice(&fs::read(status_dir.join("test-session.json")).unwrap()).unwrap();
    assert_eq!(raw["other"], "untouched");
    let mut entries: Vec<_> = fs::read_dir(&status_dir)
        .unwrap()
        .map(|p| p.unwrap().file_name())
        .collect();
    entries.sort();
    assert_eq!(entries, ["test-session.json", "test-session.lock"]);
}

#[test]
fn real_stdin_entrypoint_fails_open_for_malformed_payloads() {
    let f = Fixture::new();
    for input in [
        "not json",
        "null",
        "[]",
        "1",
        r#"{"tool_name":"Bash","tool_input":null}"#,
        r#"{"session_id":"../escape","hook_event_name":"Stop"}"#,
        r#"{"session_id":42,"hook_event_name":"Stop"}"#,
    ] {
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
            .write_all(input.as_bytes())
            .unwrap();
        let out = child.wait_with_output().unwrap();
        assert_eq!(out.status.code(), Some(0), "{input}: {out:?}");
        assert!(out.stdout.is_empty());
        assert!(out.stderr.is_empty());
    }
    assert!(!f.config.state_dir.exists());
}

#[test]
fn counter_io_errors_never_block_but_recognized_polling_still_does() {
    let mut f = Fixture::new();
    fs::write(f.dir.path().join("not-dir"), "occupied").unwrap();
    f.config.state_dir = f.dir.path().join("not-dir/state");
    assert_eq!(
        handle(
            &json!({"session_id":"test","hook_event_name":"Stop"}),
            &f.config,
            START
        ),
        None
    );
    let denied = handle(
        &json!({"hook_event_name":"PreToolUse", "tool_name":"Bash",
        "tool_input":{"command":"while true; do sleep 2; git status; done"}}),
        &f.config,
        START,
    );
    assert!(denied
        .unwrap()
        .contains("python -m plangraph.spawner.cli wait <run_id>"));
}

#[test]
fn stdin_entrypoint_counts_and_denies_without_stdout() {
    let f = Fixture::new();
    f.call("message-1", 1);
    let run = |payload: Value| {
        let mut child = Command::new(env!("CARGO_BIN_EXE_turn-budget-hook"))
            .env_clear()
            .env("HOME", f.dir.path())
            .env("FLEET_STATE_DIR", &f.config.state_dir)
            .env("FLEET_SESSION_NAME", "build-L1-0429")
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
    };
    let payload = json!({"session_id":"test-session", "hook_event_name":"Stop",
        "transcript_path":f.dir.path().join("transcript.jsonl")});
    let out = run(payload);
    assert!(out.status.success());
    assert!(out.stdout.is_empty() && out.stderr.is_empty());
    assert_eq!(f.status()["wake_calls"], 1);
    let payload = json!({"session_id":"test-session", "hook_event_name":"PreToolUse",
        "tool_name":"Bash", "tool_input":{"command":"sleep 5; git status"}});
    let out = run(payload);
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert_eq!(String::from_utf8(out.stderr).unwrap().lines().count(), 1);
}

#[test]
fn refused_sleep_poll_still_counts_the_model_response() {
    let f = Fixture::new();
    f.call("poll-attempt", 1);
    let payload = json!({"session_id":"test-session", "hook_event_name":"PreToolUse",
        "transcript_path":f.dir.path().join("transcript.jsonl"),
        "tool_name":"Bash", "tool_input":{"command":"sleep 2; git status"}});
    assert!(handle(&payload, &f.config, START + 1).is_some());
    assert_eq!(f.status()["wake_calls"], 1);
    assert_eq!(f.status()["hour_calls"], 1);
}

#[test]
fn corrupt_status_is_preserved_and_a_busy_writer_never_blocks() {
    let f = Fixture::new();
    let dir = f.config.state_dir.join("status");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("test-session.json");
    fs::write(&path, "broken status").unwrap();
    f.event("Stop", START);
    assert_eq!(fs::read_to_string(&path).unwrap(), "broken status");
    fs::write(&path, "{}").unwrap();
    let guard = pg_worker_core::lockfile::try_lock(&dir.join("test-session.lock"))
        .unwrap()
        .unwrap();
    f.call("one", 1);
    f.event("Stop", START + 1);
    assert_eq!(fs::read_to_string(&path).unwrap(), "{}");
    drop(guard);
    f.event("Stop", START + 1);
    assert_eq!(f.status()["wake_calls"], 1);
}

#[test]
fn timestamps_in_test_transcripts_are_valid_beyond_the_first_hour() {
    let f = Fixture::new();
    f.call("later", 7201);
    f.event("Stop", START + 7300);
    assert_eq!(f.status()["calls"], json!([START + 7201]));
}

#[cfg(unix)]
#[test]
fn atomic_replacement_preserves_existing_status_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    f.event("Stop", START);
    let path = f.config.state_dir.join("status/test-session.json");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    f.event("Stop", START + 1);
    assert_eq!(
        fs::metadata(path).unwrap().permissions().mode() & 0o777,
        0o640
    );
}

#[test]
fn bounded_message_history_does_not_freeze_long_session_accounting() {
    let f = Fixture::new();
    for i in 0..10_000 {
        f.call(&format!("{i:05}{}", "x".repeat(220)), 1);
    }
    f.event("Stop", START + 1);
    assert_eq!(f.status()["wake_calls"], 10_000);
    assert_eq!(f.status()["seen"].as_array().unwrap().len(), 2048);
    assert_eq!(f.status()["calls"].as_array().unwrap().len(), 8192);
    f.call("next", 2);
    f.event("Stop", START + 2);
    assert_eq!(f.status()["wake_calls"], 10_001);
    f.call("next", 2);
    f.event("Stop", START + 2);
    assert_eq!(f.status()["wake_calls"], 10_001);
}

#[test]
fn oversized_stdin_exits_silently_without_creating_state() {
    let f = Fixture::new();
    // Valid JSON just over the cap, so accepting/truncating the cap cannot hide
    // behind the malformed-JSON path. Exactly cap+1 bytes avoids a pipe race.
    let prefix = r#"{"session_id":"test-session","hook_event_name":"Stop","padding":""#;
    let input = format!(
        "{prefix}{}\"}}",
        "x".repeat(8 * 1024 * 1024 + 1 - prefix.len() - 2)
    );
    assert!(serde_json::from_str::<Value>(&input).is_ok());
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
        .write_all(input.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(0));
    assert!(out.stdout.is_empty() && out.stderr.is_empty());
    assert!(!f.config.state_dir.exists());
}

#[test]
fn legacy_near_cap_histories_are_compacted_before_the_next_update() {
    let f = Fixture::new();
    f.event("Stop", START);
    let path = f.config.state_dir.join("status/test-session.json");
    let mut raw: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    raw["turn_budget"]["seen"] = json!((0..4000)
        .map(|i| format!("{i:05}{}", "x".repeat(220)))
        .collect::<Vec<_>>());
    fs::write(&path, raw.to_string()).unwrap();
    assert!(fs::metadata(&path).unwrap().len() < 1024 * 1024);
    f.call("new", 1);
    f.event("Stop", START + 1);
    assert_eq!(f.status()["wake_calls"], 1);
    assert_eq!(f.status()["seen"].as_array().unwrap().len(), 2048);
    assert!(fs::metadata(&path).unwrap().len() < 200_000);
}
