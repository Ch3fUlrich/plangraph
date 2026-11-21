use std::path::Path;
use std::process::{Command, Output, Stdio};

fn run(root: &Path, script: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_guard-hook"))
        .args(["context", "250"])
        .arg(script)
        .env("FLEET_STATE_DIR", root)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

#[test]
fn blocks_missing_crashed_or_timed_out_guard_and_opens_one_incident() {
    let state = tempfile::tempdir().unwrap();
    let script = state.path().join("fixture.py");
    for (body, reason) in [
        (None, "guard file unavailable"),
        (Some("raise RuntimeError('private crash')\n"), "crashed"),
        (Some("import time; time.sleep(10)\n"), "timed out"),
    ] {
        if let Some(body) = body {
            std::fs::write(&script, body).unwrap();
        }
        let first = run(state.path(), &script);
        let again = run(state.path(), &script);
        assert_eq!(first.status.code(), Some(2));
        assert_eq!(again.status.code(), Some(2));
        assert!(first.stdout.is_empty() && again.stdout.is_empty());
        assert_eq!(
            String::from_utf8_lossy(&first.stderr),
            format!("GUARD-OUTAGE context {reason}\n")
        );
        assert!(!String::from_utf8_lossy(&again.stderr).contains("GUARD-OUTAGE"));
        let marker = state.path().join("guard-outages/context.alert");
        let healthy = r#"print('{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"fixture"}}')"#;
        std::fs::write(&script, healthy).unwrap();
        let output = run(state.path(), &script);
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
