use pg_worker_core::turn_budget::{handle, Config};
use serde_json::{json, Value};

fn event(command: &str, name: &str) -> Value {
    json!({"session_id":"test-session", "hook_event_name":name, "tool_name":"Bash",
        "tool_input":{"command":command}, "tool_response":{"exit_code":0}})
}
fn config(dir: &tempfile::TempDir) -> Config {
    Config {
        state_dir: dir.path().join("state"),
        launch_dir: dir.path().join("launch"),
        session_name: Some("build-L1-0429".into()),
    }
}

#[test]
fn sleep_polling_shapes_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "while true; do git status; sleep 5; done",
        "until test -f done; do sleep 1; done",
        "sleep 10; git status --short",
        "sleep 2 && cat status.json",
        "sleep 2\ngh pr checks 42",
        "bash -c 'while true; do sleep 1; git status; done'",
        "while true; do /bin/sleep 1; done",
    ] {
        let line = handle(&event(command, "PreToolUse"), &c, 1).unwrap();
        assert!(line.contains("run_in_background=true"), "{command}: {line}");
    }
}

#[test]
fn recognised_polling_survives_broken_transcript_and_missing_session_metadata() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let mut check = event("gh pr checks 42", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    check["transcript_path"] = json!(dir.path()); // a directory cannot be a transcript
    assert!(handle(&check, &c, 2).is_some());
    let mut looped = event("while true; do sleep 1; done", "PreToolUse");
    looped["session_id"] = json!(false);
    assert!(handle(&looped, &c, 3).is_some());
}

#[test]
fn quoted_literals_comments_and_file_arguments_are_not_sleep_commands() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "cat sleep; git status",
        "echo sleep; git status",
        "cat <<'EOF'\nwhile true; do sleep 1; done\nEOF",
        "echo \"$(cat state)\"",
        "git status # sleep 1; git status",
    ] {
        assert_eq!(
            handle(&event(command, "PreToolUse"), &c, 1),
            None,
            "{command}"
        );
    }
}

#[test]
fn ordinary_commands_and_single_exiting_background_wait_pass() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "cargo test",
        "echo 'while true; do sleep 1; done'",
        "printf '%s' 'sleep 2; git status'",
        "# while sleep\necho ok",
        "sleep 1",
        "sleep 1; touch ready",
        "sleep 1; touch ready; git status",
        "git commit -m 'sleep 1; git status'",
        "python -m plangraph.spawner.cli wait fake-run --timeout 60 &",
        "wait 123",
    ] {
        assert_eq!(
            handle(&event(command, "PreToolUse"), &c, 1),
            None,
            "{command}"
        );
    }
    let mut wait = event(
        "python -m plangraph.spawner.cli wait fake-run --timeout 60",
        "PreToolUse",
    );
    wait["tool_input"]["run_in_background"] = json!(true);
    for _ in 0..2 {
        assert_eq!(handle(&wait, &c, 1), None);
    }
    let exiting_wait = event("gh pr checks 42 --watch", "PreToolUse");
    for _ in 0..2 {
        assert_eq!(handle(&exiting_wait, &c, 1), None);
    }
}

#[test]
fn unchanged_check_is_refused_on_second_call_until_successful_state_change() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let check = event("git status --short", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    assert!(handle(&check, &c, 2).is_some());
    assert_eq!(handle(&event("git log -1", "PreToolUse"), &c, 3), None);
    assert!(
        handle(&check, &c, 4).is_some(),
        "another read is not a state change"
    );
    assert_eq!(
        handle(&event("echo still waiting", "PostToolUse"), &c, 5),
        None
    );
    assert!(handle(&check, &c, 6).is_some());
    assert_eq!(handle(&event("touch changed", "PreToolUse"), &c, 7), None);
    assert!(
        handle(&check, &c, 8).is_some(),
        "pre-tool is not successful execution"
    );
    let mut failed = event("touch changed", "PostToolUse");
    failed["tool_response"]["exit_code"] = json!(1);
    assert_eq!(handle(&failed, &c, 9), None);
    assert!(handle(&check, &c, 10).is_some());
    assert_eq!(handle(&event("touch changed", "PostToolUse"), &c, 11), None);
    assert_eq!(handle(&check, &c, 12), None);
    assert!(handle(&check, &c, 13).is_some());
}

#[test]
fn edits_clear_poll_history_but_wakes_and_different_sessions_do_not_confuse_it() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let check = event("cat status.json", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    let mut other = check.clone();
    other["session_id"] = json!("other-session");
    assert_eq!(handle(&other, &c, 2), None);
    assert_eq!(
        handle(
            &json!({"session_id":"test-session", "hook_event_name":"Notification"}),
            &c,
            3
        ),
        None
    );
    assert!(handle(&check, &c, 4).is_some());
    assert_eq!(
        handle(
            &json!({"session_id":"test-session", "hook_event_name":"PostToolUse",
        "tool_name":"Edit", "tool_response":{}}),
            &c,
            5
        ),
        None
    );
    assert_eq!(handle(&check, &c, 6), None);
}

#[test]
fn malformed_metadata_never_denies_an_ordinary_command() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for payload in [
        json!(null),
        json!([]),
        json!({"tool_name":"Bash","tool_input":false}),
        json!({"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":3}}),
        json!({"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo '"}}),
    ] {
        assert_eq!(handle(&payload, &c, 1), None);
    }
}
