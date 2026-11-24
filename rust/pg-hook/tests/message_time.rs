use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

#[path = "../../test_support/python.rs"]
mod test_python;

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
            json!({"message": "Created a minute ago, no stamp"}),
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
fn placeholders_are_refused_with_or_without_about() {
    for (text, code) in [
        ("Created about 00:0xZ", 2),
        ("Created 00:0xZ", 2),
        ("Created 05:4xZ", 2),
        ("Created about 05:2xZ", 2),
        ("No stamp here", 0),
    ] {
        assert_eq!(
            run("SendMessage", json!({"message": text})).status.code(),
            Some(code)
        );
    }
}

#[test]
fn dated_stamps_must_be_current_and_dated_placeholders_are_refused() {
    for (text, code) in [
        ("Created 2020-01-01T04:02:00Z", 2),
        ("Created 2026-11-24T05:4xZ", 2),
        ("Created 2026-11-24 05:4x:00+01:00", 2),
        ("Created on 2020-01-01", 0),
    ] {
        let output = run("SendMessage", json!({"message": text}));
        assert_eq!(output.status.code(), Some(code), "{text}");
    }
    let now = chrono_free_now_iso();
    let output = run("SendMessage", json!({"message": format!("Created {now}")}));
    assert_eq!(output.status.code(), Some(0), "{now}");
}

/// The current UTC instant as `YYYY-MM-DDTHH:MM:SSZ`, from the system clock.
fn chrono_free_now_iso() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let (days, time) = (seconds.div_euclid(86400), seconds.rem_euclid(86400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        time / 3600,
        (time / 60) % 60,
        time % 60
    )
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
    let output = test_python::command()
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
    let output = test_python::command()
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

#[test]
fn a_stamp_ten_minutes_off_either_way_is_refused_and_the_current_minute_passes() {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        % 86400;
    let stamp = |seconds: u64| format!("{:02}:{:02}Z", seconds / 3600, (seconds / 60) % 60);
    assert!(run(
        "SendMessage",
        json!({"message": format!("Merged at {}", stamp(now))})
    )
    .status
    .success());
    let behind = stamp((now + 86400 - 600) % 86400);
    let stale = run(
        "SendMessage",
        json!({"message": format!("Merged at {behind}")}),
    );
    assert_eq!(stale.status.code(), Some(2));
    assert!(String::from_utf8(stale.stderr).unwrap().contains(&behind));
    if now < 86400 - 720 {
        let ahead = stamp(now + 600);
        let refused = run(
            "SendMessage",
            json!({"message": format!("Merged at {ahead}")}),
        );
        assert_eq!(refused.status.code(), Some(2));
        assert!(String::from_utf8(refused.stderr).unwrap().contains(&ahead));
    }
}
