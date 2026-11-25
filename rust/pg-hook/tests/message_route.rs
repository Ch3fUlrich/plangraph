#![cfg(unix)]
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

fn run(state: &Path, session: &str, to: &str, message: &str) -> Output {
    run_with(state, None, session, to, message)
}

/// `state` is the XDG state home, so the routing inbox is `state/routing/inbox` as `routerctl`
/// finds it; the launch records are in `state/routing` unless `ROUTING_STATE_DIR` is moved.
fn run_with(state: &Path, inbox: Option<&Path>, session: &str, to: &str, message: &str) -> Output {
    run_split(state, &state.join("routing"), inbox, session, to, message)
}

fn run_split(
    state: &Path,
    routing_state: &Path,
    inbox: Option<&Path>,
    session: &str,
    to: &str,
    message: &str,
) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pg-hook"));
    command
        .arg("message-route")
        .env("XDG_STATE_HOME", state)
        .env("ROUTING_STATE_DIR", routing_state)
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
    let inbox = state.path().join("routing/inbox");
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
    let dir = state.join("routing/launch/sessions");
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
    let inbox = fs::read_to_string(state.path().join("routing/inbox/L0.md")).unwrap();
    assert_eq!(inbox.lines().count(), 10);
    let first = inbox.lines().next().unwrap();
    assert!(
        first.starts_with("- 20") && first.contains("Z plan-order-0767: ACK train 29 merged"),
        "{first}"
    );
    let log = fs::read_to_string(state.path().join("routing/inbox/diversions.jsonl")).unwrap();
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
        fs::read_to_string(state.path().join("routing/inbox/L0.md")).unwrap(),
        ""
    );
}

#[test]
fn an_unwritable_inbox_fails_open() {
    let state = router_state();
    let inbox = state.path().join("routing/inbox/L0.md");
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
    assert!(!state.path().join("routing/inbox").exists());
    fs::create_dir_all(state.path().join("routing/inbox")).unwrap();
    assert_eq!(
        run(state.path(), "s", "routing", "DONE x").status.code(),
        Some(0)
    );
    assert!(!state.path().join("routing/inbox/L0.md").exists());
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
    assert!(!state.path().join("routing/inbox").exists());
}

#[test]
fn a_file_the_router_has_no_offset_for_is_created_empty_and_the_first_lines_are_sent() {
    let state = tempfile::tempdir().unwrap();
    let inbox = state.path().join("routing/inbox");
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
    fs::create_dir(state.path().join("routing/inbox/diversions.jsonl")).unwrap();
    let output = run(state.path(), "s", "routing", "DONE x");
    assert_eq!(output.status.code(), Some(2), "{}", stderr(&output));
    assert!(stderr(&output).contains("diversion log failed"));
    let text = fs::read_to_string(state.path().join("routing/inbox/L0.md")).unwrap();
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

/// What the router heartbeat does with the inbox (`routerctl new_inbox_lines`): a file with no
/// saved offset, or one saved beyond the file size, restarts at the end and skips what is there;
/// otherwise the bytes after the offset are read and the offset moves to the end.
fn heartbeat_reads(inbox_dir: &Path) -> Vec<String> {
    let offsets_path = inbox_dir.join("offsets.json");
    let mut offsets: serde_json::Map<String, Value> =
        serde_json::from_slice(&fs::read(&offsets_path).unwrap()).unwrap();
    let file = inbox_dir.join("L0.md");
    let size = fs::metadata(&file).unwrap().len();
    let key = file.to_str().unwrap().to_owned();
    let start = offsets.get(&key).and_then(Value::as_u64);
    let mut lines = Vec::new();
    match start {
        Some(start) if start <= size => {
            let bytes = fs::read(&file).unwrap();
            let text = String::from_utf8_lossy(&bytes[start as usize..]).into_owned();
            lines.extend(text.lines().map(str::to_owned));
        }
        _ => {}
    }
    offsets.insert(key, json!(size));
    fs::write(&offsets_path, Value::Object(offsets).to_string()).unwrap();
    lines
}

#[test]
fn a_saved_offset_beyond_the_file_sends_the_message_until_the_heartbeat_resets_it() {
    let state = router_state();
    let dir = state.path().join("routing/inbox");
    let file = dir.join("L0.md");
    fs::write(&file, "- 17 bytes of old").unwrap();
    assert_eq!(fs::metadata(&file).unwrap().len(), 17);
    fs::write(
        dir.join("offsets.json"),
        json!({file.to_str().unwrap(): 500}).to_string(),
    )
    .unwrap();
    // The reader would start at the end and skip a line filed now: the message is sent instead.
    let sent = run(state.path(), "s", "routing", "DONE while truncated");
    assert_eq!(sent.status.code(), Some(0), "{}", stderr(&sent));
    assert_eq!(fs::read_to_string(&file).unwrap(), "- 17 bytes of old");
    assert!(!dir.join("diversions.jsonl").exists());
    // After the heartbeat has reset its offset the next line is filed and the reader gets it.
    assert!(heartbeat_reads(&dir).is_empty());
    let filed = run(state.path(), "s", "routing", "DONE after reset");
    assert_eq!(filed.status.code(), Some(2), "{}", stderr(&filed));
    let read = heartbeat_reads(&dir);
    assert_eq!(read.len(), 1, "{read:?}");
    assert!(read[0].ends_with("DONE after reset"), "{read:?}");
}

#[test]
fn an_offset_that_is_not_a_number_sends_the_message() {
    for offset in [json!(null), json!("0"), json!(-1), json!(1.5)] {
        let state = router_state();
        let dir = state.path().join("routing/inbox");
        let key = dir.join("L0.md");
        fs::write(
            dir.join("offsets.json"),
            json!({key.to_str().unwrap(): offset}).to_string(),
        )
        .unwrap();
        let output = run(state.path(), "s", "routing", "DONE x");
        assert_eq!(output.status.code(), Some(0), "{offset}");
        assert_eq!(fs::read_to_string(&key).unwrap(), "");
    }
}

#[test]
fn the_routing_state_dir_setting_does_not_move_the_inbox() {
    let state = router_state(); // the XDG state home, with the inbox the router reads
    let elsewhere = tempfile::tempdir().unwrap(); // ROUTING_STATE_DIR: launch records only
    let output = run_split(
        state.path(),
        elsewhere.path(),
        None,
        "s",
        "routing",
        "DONE x",
    );
    assert_eq!(output.status.code(), Some(2), "{}", stderr(&output));
    let dir = state.path().join("routing/inbox");
    assert_eq!(heartbeat_reads(&dir).len(), 1);
    assert!(fs::read_dir(elsewhere.path()).unwrap().next().is_none());
}

#[test]
fn a_message_the_time_guard_refuses_is_not_filed() {
    let state = router_state();
    let dir = state.path().join("routing/inbox");
    for text in [
        "DONE at 05:4xZ",
        "DONE about 05:2xZ",
        "DONE at 2026-11-24T05:4xZ",
        "DONE merged 2020-01-01T04:02:00Z",
        "DONE at 2099-01-01T04:02:00Z",
    ] {
        let output = run(state.path(), "s", "routing", text);
        assert_eq!(output.status.code(), Some(2), "{text}");
        assert!(stderr(&output).starts_with("message-time: "), "{text}");
    }
    assert_eq!(fs::read_to_string(dir.join("L0.md")).unwrap(), "");
    assert!(!dir.join("diversions.jsonl").exists());
}
