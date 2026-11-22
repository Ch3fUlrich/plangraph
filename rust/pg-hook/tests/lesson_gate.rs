use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Output, Stdio};

fn run(transcript: &[Value], ledger: &str) -> Output {
    let dir = tempfile::tempdir().unwrap();
    let transcript_path = dir.path().join("transcript.jsonl");
    let ledger_path = dir.path().join("lesson-session.tsv");
    std::fs::write(
        &transcript_path,
        transcript
            .iter()
            .map(|row| format!("{row}\n"))
            .collect::<String>(),
    )
    .unwrap();
    std::fs::write(&ledger_path, ledger).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .arg("lesson-stop")
        .env("PG_LESSON_LEDGER_DIR", dir.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(
        child.stdin.take().unwrap(),
        "{}",
        json!({
            "hook_event_name": "Stop", "session_id": "session", "transcript_path": transcript_path,
        })
    )
    .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn stop_requires_a_ledger_entry_for_each_signal() {
    let transcript = [
        json!({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool1", "name": "Bash"},
            {"type": "tool_use", "id": "tool2", "name": "mcp__keeper__define"}
        ]}}),
        json!({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "is_error": true},
            {"type": "tool_result", "tool_use_id": "tool2", "content": "Refused: claim held"}
        ]}}),
        json!({"type": "user", "message": {"content": "<teammate-message teammate_id=\"peer\">BLOCKED: no response</teammate-message>"}}),
        json!({"type": "user", "message": {"content": "Correction: use the logged time."}}),
        json!({"type": "assistant", "message": {"content": "FAIL: this is the assistant's report"}}),
    ];
    let partial =
        "tool:tool1\tplangraph:LE-COMMAND\ntool:tool2\tattached to plangraph:KEEPER-HYGIENE\n";
    let result = run(&transcript, partial);
    assert_eq!(result.status.code(), Some(2));
    let stderr = String::from_utf8(result.stderr).unwrap();
    assert!(stderr.contains("message:3:1 peer BLOCKED"), "{stderr}");
    assert!(
        stderr.contains("message:4:1 operator correction"),
        "{stderr}"
    );
    assert!(!stderr.contains("tool:tool1"), "{stderr}");
    assert!(!stderr.contains("tool:tool2"), "{stderr}");
    let complete = format!("{partial}message:3:1\tLE-PEER\nmessage:4:1\tLE-TIME\n");
    assert!(run(&transcript, &complete).status.success());
}

#[test]
fn codex_tool_exits_are_checked_on_stop() {
    let transcript = [
        json!({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "call_one"}}),
        json!({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_one", "output": "Chunk ID: abc\nProcess exited with code 1\nOutput:\nfixture"}}),
    ];
    let result = run(&transcript, "tool:call_one\tunrelated keeper item\n");
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8(result.stderr)
        .unwrap()
        .contains("tool:call_one"));
}
