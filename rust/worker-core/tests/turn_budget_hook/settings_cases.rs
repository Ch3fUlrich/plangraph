use super::*;

#[test]
fn effective_settings_measure_polls_without_live_alerts() {
    for (version, fixture) in [
        (
            "2.1.323",
            include_str!("../../../pg-hook/tests/fixtures/claude-hook-settings/2.1.323.json"),
        ),
        (
            "2.1.329",
            include_str!("../../../pg-hook/tests/fixtures/claude-hook-settings/2.1.329.json"),
        ),
    ] {
        let settings: Value = serde_json::from_str(fixture).unwrap();
        for event in [
            "UserPromptSubmit",
            "Notification",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ] {
            let groups = settings["hooks"][event].as_array().unwrap();
            let entries: Vec<_> = groups
                .iter()
                .flat_map(|group| {
                    group["hooks"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(move |hook| (group, hook))
                })
                .filter(|(_, hook)| {
                    hook["command"]
                        .as_str()
                        .is_some_and(|cmd| cmd.contains("turn-budget-hook"))
                })
                .collect();
            let [(group, hook)] = entries.as_slice() else {
                panic!("{version}: expected one turn-budget registration for {event}");
            };
            assert_eq!(group["matcher"], "", "{version}: {event}");
            assert_eq!(hook["type"], "command", "{version}: {event}");
            assert_eq!(hook["onFailure"], "continue", "{version}: {event}");
            let mut argv = hook["command"].as_str().unwrap().split_ascii_whitespace();
            assert_eq!(argv.next(), Some(r#""$HOME/.local/bin/turn-budget-hook""#));
            let args: Vec<_> = argv.collect();
            assert_eq!(args, ["--measure-only"], "{version}: {event}");
            if event == "PreToolUse" {
                assert_isolated_measurement(&args);
            }
        }
    }
}

fn assert_isolated_measurement(args: &[&str]) {
    let f = Fixture::new();
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    for index in 0..7 {
        f.append(json!({"type":"assistant", "timestamp":chrono::DateTime::from_timestamp(now as i64, 0).unwrap().to_rfc3339(),
            "message":{"id":format!("measure-{index}"),"role":"assistant","content":[]}}));
    }
    let mut child = Command::new(env!("CARGO_BIN_EXE_turn-budget-hook"))
        .args(args)
        .env("FLEET_STATE_DIR", &f.config.state_dir)
        .env("FLEET_SESSION_NAME", "build-L1-0429")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(child.stdin.take().unwrap(), "{}", json!({
        "hook_event_name":"PreToolUse","session_id":"test-session","tool_name":"Bash",
        "tool_input":{"command":"sleep 5; git status"}, "transcript_path":f.dir.path().join("transcript.jsonl")
    })).unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success());
    assert!(out.stdout.is_empty() && out.stderr.is_empty());
    assert!(pg_worker_core::turn_budget::read_alerts(&f.config.state_dir, now).is_empty());
    let shadow = f.config.state_dir.join("shadow-turn-budget");
    assert_eq!(
        pg_worker_core::turn_budget::read_alerts(&shadow, now).len(),
        1
    );
}
