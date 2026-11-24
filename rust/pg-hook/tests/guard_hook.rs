#![cfg(unix)]

use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output, Stdio};

/// A guard that must finish gets a budget no loaded host exceeds while python starts; only
/// the sleeping guard uses a short one, so "crashed" is never raced against "timed out".
const FINISH_MS: &str = "20000";
const TIMEOUT_MS: &str = "250";

fn run(root: &Path, guard: &str, script: &Path, millis: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_guard-hook"))
        .args([guard, millis])
        .arg(script)
        .env("FLEET_STATE_DIR", root)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

#[test]
fn effective_settings_block_guard_outages_once_per_incident() {
    for (version, fixture) in [
        (
            "2.1.323",
            include_str!("fixtures/claude-hook-settings/2.1.323.json"),
        ),
        (
            "2.1.329",
            include_str!("fixtures/claude-hook-settings/2.1.329.json"),
        ),
    ] {
        let settings: Value = serde_json::from_str(fixture).unwrap();
        let groups = settings["hooks"]["PreToolUse"].as_array().unwrap();
        for (guard, matcher, script) in [
            ("bash", "Bash", "bash_guard.py"),
            ("context", "", "ctx_guard_session.py"),
        ] {
            let command = format!(
                r#""$HOME/.local/bin/guard-hook" {guard} 5000 "$HOME/.local/share/plangraph/hooks/{script}""#
            );
            let entries: Vec<_> = groups
                .iter()
                .flat_map(|group| {
                    group["hooks"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(move |hook| (group, hook))
                })
                .filter(|(_, hook)| hook["command"] == command)
                .collect();
            let [(group, hook)] = entries.as_slice() else {
                panic!("{version}: expected one {guard} registration");
            };
            assert_eq!(group["matcher"], matcher, "{version}: {guard}");
            assert_eq!(hook["type"], "command", "{version}: {guard}");
            assert_eq!(hook["onFailure"], "block", "{version}: {guard}");
            assert!(hook["timeout"].as_u64().unwrap() > 5, "{version}: {guard}");
            let registered_guard = hook["command"]
                .as_str()
                .unwrap()
                .split_ascii_whitespace()
                .nth(1)
                .unwrap();
            assert_incident(registered_guard);
        }
    }
}

fn assert_incident(guard: &str) {
    let state = tempfile::tempdir().unwrap();
    let script = state.path().join("fixture.py");
    for (body, reason, millis) in [
        (None, "guard file unavailable", FINISH_MS),
        (
            Some("raise RuntimeError('private crash')\n"),
            "crashed",
            FINISH_MS,
        ),
        (
            Some("import time; time.sleep(10)\n"),
            "timed out",
            TIMEOUT_MS,
        ),
    ] {
        if let Some(body) = body {
            std::fs::write(&script, body).unwrap();
        }
        let first = run(state.path(), guard, &script, millis);
        let again = run(state.path(), guard, &script, millis);
        assert_eq!(first.status.code(), Some(2));
        assert_eq!(again.status.code(), Some(2));
        assert!(first.stdout.is_empty() && again.stdout.is_empty());
        assert_eq!(
            String::from_utf8_lossy(&first.stderr),
            format!("GUARD-OUTAGE {guard} {reason}\n")
        );
        assert!(!String::from_utf8_lossy(&again.stderr).contains("GUARD-OUTAGE"));
        let marker = state.path().join(format!("guard-outages/{guard}.alert"));
        assert_eq!(
            std::fs::read_to_string(&marker).unwrap(),
            format!("GUARD-OUTAGE {guard} {reason}\n"),
        );
        let healthy = r#"print('{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"fixture"}}')"#;
        std::fs::write(&script, healthy).unwrap();
        let output = run(state.path(), guard, &script, FINISH_MS);
        assert!(output.status.success());
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap()
                ["hookSpecificOutput"]["permissionDecision"],
            "deny"
        );
        assert!(output.stderr.is_empty());
        assert!(!marker.exists());
    }
}
