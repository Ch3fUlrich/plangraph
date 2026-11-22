use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const PYTHON: &str = if cfg!(windows) { "python" } else { "python3" };

fn run(tool_name: &str, input: Value) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-hook"))
        .arg("message-time")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(
        child.stdin.take().unwrap(),
        "{}",
        json!({
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": input,
        })
    )
    .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn future_times_are_refused_for_both_send_paths() {
    for (tool, input) in [
        (
            "SendMessage",
            json!({"message": "Slice created at 2099-01-01T04:02:00Z."}),
        ),
        (
            "Bash",
            json!({"command": "fleet-msg send peer 'Slice created at 2099-01-01T04:02:00Z.'"}),
        ),
    ] {
        let output = run(tool, input);
        assert_eq!(output.status.code(), Some(2));
        assert!(String::from_utf8(output.stderr)
            .unwrap()
            .contains("2099-01-01T04:02:00Z"));
    }
}

#[test]
fn only_send_text_is_subject_to_the_time_check() {
    for (tool, input) in [
        (
            "SendMessage",
            json!({"message": "Created 2020-01-01T04:02:00Z"}),
        ),
        (
            "Bash",
            json!({"command": "printf '%s' 'fleet-msg send peer 2099-01-01T04:02:00Z'"}),
        ),
        (
            "Bash",
            json!({"command": "fleet-msg read --since 2099-01-01T04:02:00Z"}),
        ),
        ("Read", json!({"file_path": "2099-01-01T04:02:00Z"})),
    ] {
        assert!(run(tool, input).status.success());
    }
}

#[test]
fn placeholders_require_about() {
    for (text, code) in [("Created about 00:0xZ", 0), ("Created 00:0xZ", 2)] {
        assert_eq!(
            run("SendMessage", json!({"message": text})).status.code(),
            Some(code)
        );
    }
}

#[test]
fn time_of_day_uses_the_utc_clock() {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        % 86400;
    let utc_time = |seconds| {
        format!(
            "{:02}:{:02}:{:02}Z",
            seconds / 3600,
            (seconds / 60) % 60,
            seconds % 60
        )
    };
    assert!(run(
        "SendMessage",
        json!({"message": utc_time(now.saturating_sub(90))})
    )
    .status
    .success());
    if now < 86400 - 180 {
        let time = utc_time(now + 180);
        let refused = run("SendMessage", json!({"message": time}));
        assert_eq!(refused.status.code(), Some(2));
        assert!(String::from_utf8(refused.stderr).unwrap().contains(&time));
    }
}

#[test]
fn fleet_client_checks_the_final_message_before_loading_transport() {
    let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = Command::new(PYTHON)
        .args([
            "-m",
            "plangraph.fleet_bus.client",
            "send",
            "peer",
            "Created 2099-01-01T04:02:00Z",
        ])
        .env("PYTHONPATH", source)
        .env("PG_MESSAGE_TIME_HOOK", env!("CARGO_BIN_EXE_pg-hook"))
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let diagnostic = String::from_utf8(output.stderr).unwrap();
    assert!(
        diagnostic.contains("message-time: 2099-01-01T04:02:00Z"),
        "{diagnostic}"
    );
}

#[test]
fn fleet_mcp_refuses_future_time_before_loading_transport() {
    let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = Command::new(PYTHON)
        .args([
            "-c",
            r#"
from unittest.mock import patch
from plangraph.keeper.mcp import _call_tool

params = {"name": "fleet_msg_send", "arguments": {
    "to": "peer", "text": "Created 2099-01-01T04:02:00Z",
}}
with patch("plangraph.fleet_bus.client.build_client", side_effect=AssertionError("transport loaded")):
    reply = _call_tool(1, params, None)
result = reply["result"]
assert result["isError"], result
assert "message-time: 2099-01-01T04:02:00Z" in result["content"][0]["text"], result
"#,
        ])
        .env("PYTHONPATH", source)
        .env("PG_MESSAGE_TIME_HOOK", env!("CARGO_BIN_EXE_pg-hook"))
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
}
