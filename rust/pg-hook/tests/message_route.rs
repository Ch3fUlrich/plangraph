#![cfg(unix)]
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

fn run(state: &Path, session: &str, to: &str, message: &str) -> Output {
    run_with(state, None, session, to, message)
}

fn run_with(state: &Path, inbox: Option<&Path>, session: &str, to: &str, message: &str) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pg-hook"));
    command
        .arg("message-route")
        .env("ROUTING_STATE_DIR", state)
        .env_remove("ROUTER_INBOX_DIR")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(inbox) = inbox {
        command.env("ROUTER_INBOX_DIR", inbox);
    }
    let mut child = command.spawn().unwrap();
    let payload: Value = json!({
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": message},
    });
    writeln!(child.stdin.take().unwrap(), "{payload}").unwrap();
    child.wait_with_output().unwrap()
}

/// A state dir whose inbox the router already reads: `routerctl` has an offset for `L0.md`.
fn router_state() -> tempfile::TempDir {
    let state = tempfile::tempdir().unwrap();
    let inbox = state.path().join("inbox");
    fs::create_dir_all(&inbox).unwrap();
    let key = inbox.join("L0.md");
    fs::write(&key, "").unwrap();
    fs::write(
        inbox.join("offsets.json"),
        json!({key.to_str().unwrap(): 0}).to_string(),
    )
    .unwrap();
    state
}

fn launched(state: &Path, name: &str, session_id: &str) {
    let dir = state.join("launch/sessions");
    fs::create_dir_all(&dir).unwrap();
    let record = json!({"session_name": name, "session_id": session_id});
    fs::write(dir.join(format!("{name}.json")), record.to_string()).unwrap();
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).unwrap()
}

#[test]
fn routine_lines_to_routing_land_in_the_inbox_and_are_counted() {
    let state = router_state();
    launched(state.path(), "plan-order-0767", "sess-1");
    for to in ["routing", "routing-0789"] {
        for word in ["ACK", "DONE", "FILED", "MERGED", "STATUS"] {
            let text = format!("{word} train 29\nmerged");
            let output = run(state.path(), "sess-1", to, &text);
            assert_eq!(output.status.code(), Some(2), "{to} {word}");
            assert!(stderr(&output).contains("L0.md"), "{}", stderr(&output));
        }
    }
    let inbox = fs::read_to_string(state.path().join("inbox/L0.md")).unwrap();
    assert_eq!(inbox.lines().count(), 10);
    let first = inbox.lines().next().unwrap();
    assert!(
        first.starts_with("- 20") && first.contains("Z plan-order-0767: ACK train 29 merged"),
        "{first}"
    );
    let log = fs::read_to_string(state.path().join("inbox/diversions.jsonl")).unwrap();
    let rows: Vec<Value> = log
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 10);
    assert_eq!(rows[0]["sender"], "plan-order-0767");
    assert_eq!(rows[0]["word"], "ACK");
    assert_eq!(rows[0]["day"].as_str().unwrap().len(), 10);
}

#[test]
fn urgent_and_other_messages_pass_without_a_line() {
    let state = router_state();
    for (to, text) in [
        ("routing", "BLOCKED review no leg answered"),
        ("routing", "GO-REQ train 30"),
        ("routing", "FAIL push refused"),
        ("routing", "INCIDENT host down"),
        ("routing", "Operator relay: D-1 go ahead"),
        ("plan-order", "DONE x"),
        ("writer-0762", "ACK"),
    ] {
        assert_eq!(
            run(state.path(), "s", to, text).status.code(),
            Some(0),
            "{to} {text}"
        );
    }
    assert_eq!(
        fs::read_to_string(state.path().join("inbox/L0.md")).unwrap(),
        ""
    );
}

#[test]
fn an_unwritable_inbox_fails_open() {
    let state = router_state();
    let inbox = state.path().join("inbox/L0.md");
    fs::remove_file(&inbox).unwrap();
    fs::create_dir(&inbox).unwrap(); // appending to a directory fails
    assert_eq!(
        run(state.path(), "s", "routing", "DONE x").status.code(),
        Some(0)
    );
}

#[test]
fn a_host_the_router_does_not_read_sends_the_message() {
    // No inbox directory, then a directory the router keeps no offsets in.
    let state = tempfile::tempdir().unwrap();
    assert_eq!(
        run(state.path(), "s", "routing", "DONE x").status.code(),
        Some(0)
    );
    assert!(!state.path().join("inbox").exists());
    fs::create_dir_all(state.path().join("inbox")).unwrap();
    assert_eq!(
        run(state.path(), "s", "routing", "DONE x").status.code(),
        Some(0)
    );
    assert!(!state.path().join("inbox/L0.md").exists());
}

#[test]
fn the_router_inbox_dir_setting_names_the_directory_that_is_filled() {
    let state = tempfile::tempdir().unwrap(); // no inbox under the routing state dir
    let router = tempfile::tempdir().unwrap();
    let key = router.path().join("L0.md");
    fs::write(&key, "").unwrap();
    fs::write(
        router.path().join("offsets.json"),
        json!({key.to_str().unwrap(): 0}).to_string(),
    )
    .unwrap();
    let output = run_with(
        state.path(),
        Some(router.path()),
        "s",
        "routing",
        "MERGED x",
    );
    assert_eq!(output.status.code(), Some(2), "{}", stderr(&output));
    let line = fs::read_to_string(&key).unwrap();
    assert!(line.contains("MERGED x"), "{line}");
    assert!(router.path().join("diversions.jsonl").exists());
    assert!(!state.path().join("inbox").exists());
}

#[test]
fn a_file_the_router_has_no_offset_for_is_created_empty_and_the_first_lines_are_sent() {
    let state = tempfile::tempdir().unwrap();
    let inbox = state.path().join("inbox");
    fs::create_dir_all(&inbox).unwrap();
    fs::write(inbox.join("offsets.json"), "{}").unwrap();
    // The heartbeat has no offset for L0.md, so it would start at the end and skip a diverted line.
    assert_eq!(
        run(state.path(), "s", "routing", "DONE first")
            .status
            .code(),
        Some(0)
    );
    assert_eq!(fs::read_to_string(inbox.join("L0.md")).unwrap(), "");
    // Once the heartbeat has recorded the offset, lines are diverted and every one is kept.
    let key = inbox.join("L0.md");
    fs::write(
        inbox.join("offsets.json"),
        json!({key.to_str().unwrap(): 0}).to_string(),
    )
    .unwrap();
    for word in ["DONE second", "DONE third"] {
        assert_eq!(
            run(state.path(), "s", "routing", word).status.code(),
            Some(2)
        );
    }
    let text = fs::read_to_string(&key).unwrap();
    assert_eq!(text.lines().count(), 2, "{text}");
    assert!(text.lines().next().unwrap().ends_with("DONE second"));
}

#[test]
fn a_failed_diversion_row_leaves_the_line_filed_and_the_send_refused() {
    let state = router_state();
    fs::create_dir(state.path().join("inbox/diversions.jsonl")).unwrap();
    let output = run(state.path(), "s", "routing", "DONE x");
    assert_eq!(output.status.code(), Some(2), "{}", stderr(&output));
    assert!(stderr(&output).contains("diversion log failed"));
    let text = fs::read_to_string(state.path().join("inbox/L0.md")).unwrap();
    assert_eq!(text.lines().count(), 1, "{text}");
}

#[test]
fn malformed_payloads_fail_open() {
    let state = tempfile::tempdir().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .arg("message-route")
        .env("ROUTING_STATE_DIR", state.path())
        .stdin(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(b"not json").unwrap();
    assert_eq!(child.wait().unwrap().code(), Some(0));
}
