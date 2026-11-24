#![cfg(unix)]
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

fn run(state: &Path, session: &str, to: &str, message: &str) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .arg("message-route")
        .env("ROUTING_STATE_DIR", state)
        .env_remove("PG_MESSAGE_SINGLETON_ROLES")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let payload: Value = json!({
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": message},
    });
    writeln!(child.stdin.take().unwrap(), "{payload}").unwrap();
    child.wait_with_output().unwrap()
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
    let state = tempfile::tempdir().unwrap();
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
fn urgent_and_other_messages_pass_without_a_file() {
    let state = tempfile::tempdir().unwrap();
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
    assert!(!state.path().join("inbox").exists());
}

#[test]
fn an_unwritable_inbox_fails_open() {
    let state = tempfile::tempdir().unwrap();
    fs::write(state.path().join("inbox"), "a file, not a directory").unwrap();
    assert_eq!(
        run(state.path(), "s", "routing", "DONE x").status.code(),
        Some(0)
    );
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

#[test]
fn a_stale_counter_name_is_refused_with_the_live_name() {
    let state = tempfile::tempdir().unwrap();
    launched(state.path(), "routing-0771", "a");
    launched(state.path(), "routing-0789", "b");
    launched(state.path(), "plan-order-0757", "c");
    launched(state.path(), "plan-order-0767", "d");
    for (to, live) in [
        ("routing-0743", "routing-0789"),
        ("routing-0771", "routing-0789"),
        ("plan-order-0757", "plan-order-0767"),
    ] {
        let output = run(state.path(), "s", to, "BLOCKED x");
        assert_eq!(output.status.code(), Some(2), "{to}");
        assert!(
            stderr(&output).contains(&format!("send to {live}")),
            "{}",
            stderr(&output)
        );
    }
    for (to, text) in [
        ("routing-0789", "BLOCKED x"),
        ("plan-order-0767", "hello"),
        ("plan-order", "hello"),
    ] {
        assert_eq!(
            run(state.path(), "s", to, text).status.code(),
            Some(0),
            "{to}"
        );
    }
    // Writers are many at once: an older writer name stays reachable.
    launched(state.path(), "writer-0762", "e");
    launched(state.path(), "writer-0800", "f");
    assert_eq!(
        run(state.path(), "s", "writer-0762", "hi").status.code(),
        Some(0)
    );
}

#[test]
fn a_stale_name_gets_no_diversion() {
    let state = tempfile::tempdir().unwrap();
    launched(state.path(), "routing-0789", "b");
    let output = run(state.path(), "s", "routing-0743", "DONE x");
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr(&output).contains("routing-0789"));
    assert!(!state.path().join("inbox").exists());
}
