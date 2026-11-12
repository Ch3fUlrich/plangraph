use pg_worker_core::turn_budget::{handle, Config};
use serde_json::{json, Value};

fn workspace() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("turn-budget-test-workspace")
}

fn event(command: &str, name: &str) -> Value {
    json!({"session_id":"test-session", "hook_event_name":name, "tool_name":"Bash",
        "cwd":workspace(), "tool_input":{"command":command}, "tool_response":{"exit_code":0}})
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
        "while ! test -f \"$ready\"; do sleep 1; done",
        "until [ -f \"$ready\" ]; do sleep 1; done",
        "sleep 1; cat \"$status\"",
        "while true; do sleep 2; git status; done &",
        "while git status; do :; done",
        "watch -n 5 git status",
        "timeout 2 bash -c 'while true; do sleep 1; git status; done'",
        "while true; do env git status; done",
        "for i in 1 2 3; do bash -c 'while true; do sleep 1; done'; done",
        "for i in 1 2 3; do watch git status; done",
        "for ((;;)); do git status; done",
        "for i in 1 2 3; do cat notes.txt; done; sleep 2; git status",
    ] {
        let line = handle(&event(command, "PreToolUse"), &c, 1)
            .unwrap_or_else(|| panic!("poll escaped: {command}"));
        assert!(line.contains("run_in_background=true"), "{command}: {line}");
    }
    let mut background = event("while true; do sleep 2; git status; done", "PreToolUse");
    background["tool_input"]["run_in_background"] = json!(true);
    assert!(handle(&background, &c, 1).is_some());
}

#[test]
fn recognised_polling_survives_broken_transcript_and_missing_session_metadata() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let mut check = event("gh pr checks 42", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    let mut post = event("gh pr checks 42", "PostToolUse");
    post["tool_response"]["stdout"] = json!("pending");
    assert_eq!(handle(&post, &c, 1), None);
    assert_eq!(handle(&check, &c, 1), None);
    assert_eq!(handle(&post, &c, 1), None);
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
        "cat 'do'", // a literal argument is not a shell keyword
        "git commit -m 'sleep 1; git status'",
        "python -m plangraph.spawner.cli wait fake-run --timeout 60 &",
        "wait 123",
        "timeout 2 git status", // one invocation, not a repeated check
        "while build_next; do cargo test; done",
        "for i in 1 2 3; do echo git status; done",
        "for f in *.md; do cat $f; done",
        "for i in 1 2 3; do cat notes.txt; done",
        "for i in 1 2 3; do git status; done",
        "for i in 1 2 3; do sleep 1; cat notes.txt; done",
        "while build_next; do git status; done",
        "for i in 1 2 3; do touch marker; git status; done",
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
fn quoted_shell_keywords_are_ordinary_arguments_not_control_flow() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "'while' sleep 1",
        "'until' sleep 1",
        "echo 'while'; sleep 1",
    ] {
        assert_eq!(
            handle(&event(command, "PreToolUse"), &c, 1),
            None,
            "{command}"
        );
    }
}

#[test]
fn unchanged_check_is_refused_until_a_successful_non_read_grants_one_retry() {
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
    assert_eq!(handle(&check, &c, 6), None);
    assert!(handle(&check, &c, 6).is_some(), "only one retry after echo");
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
        "tool_name":"Edit", "cwd":workspace(), "tool_input":{"file_path":"status.json"},
        "tool_response":{}}),
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

#[test]
fn bash_success_grants_one_local_retry_but_unrelated_edits_and_writes_do_not_reset_remote() {
    for (read, mutation) in [
        ("cat status.json", "touch marker"),
        ("cat status.json", "git add unrelated"),
        ("gh pr checks 42", "touch marker"),
        ("gh pr checks 42", "git add unrelated"),
        ("git status", "touch /elsewhere/marker"),
        ("git status", "git -C /elsewhere add unrelated"),
        ("cat status.json", "echo ok > marker"),
        ("cat status.json", "echo '>status.json'"),
        ("cat status.json", "cd /elsewhere; touch status.json"),
        ("cat status.json", "cp status.json unrelated.json"),
        ("cat status.json", "install status.json unrelated.json"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        let check = event(read, "PreToolUse");
        assert_eq!(handle(&check, &c, 1), None);
        if read.starts_with("gh ") {
            let mut post = event(read, "PostToolUse");
            post["tool_response"]["stdout"] = json!("unchanged remote state");
            assert_eq!(handle(&post, &c, 1), None);
            assert_eq!(handle(&check, &c, 1), None);
            assert_eq!(handle(&post, &c, 1), None);
        }
        assert!(handle(&check, &c, 2).is_some());
        assert_eq!(handle(&event(mutation, "PostToolUse"), &c, 3), None);
        if !read.starts_with("gh ") {
            assert_eq!(
                handle(&check, &c, 4),
                None,
                "local retry: {read}: {mutation}"
            );
        }
        assert!(handle(&check, &c, 4).is_some(), "{read}: {mutation}");
        let edit = json!({"session_id":"test-session", "hook_event_name":"PostToolUse",
            "tool_name":"Edit", "cwd":workspace(), "tool_input":{"file_path":"/elsewhere/status.json"},
            "tool_response":{}});
        assert_eq!(handle(&edit, &c, 5), None);
        assert!(handle(&check, &c, 6).is_some(), "unrelated edit: {read}");
    }
}

#[test]
fn bash_retry_is_local_but_changed_read_output_reenables_only_its_own_check() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let check = event("cat status.json", "PreToolUse");
    let other = event("cat other.json", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    let mut post = event("cat status.json", "PostToolUse");
    post["tool_response"]["stdout"] = json!("same");
    assert_eq!(handle(&post, &c, 1), None);
    assert_eq!(handle(&other, &c, 1), None);
    for command in ["touch status.json", "echo ok > status.json"] {
        assert_eq!(handle(&event(command, "PostToolUse"), &c, 2), None);
        assert_eq!(handle(&check, &c, 3), None);
        assert_eq!(handle(&post, &c, 3), None);
        assert!(handle(&check, &c, 4).is_some());
        assert_eq!(handle(&other, &c, 4), None);
        assert!(handle(&other, &c, 4).is_some());
    }
    assert!(
        handle(&check, &c, 7).is_some(),
        "identical output is not progress"
    );
    assert_eq!(
        handle(&event("touch status.json", "PostToolUse"), &c, 8),
        None
    );
    assert_eq!(handle(&check, &c, 8), None);
    assert_eq!(handle(&post, &c, 8), None);
    assert_eq!(handle(&other, &c, 8), None);
    assert!(handle(&other, &c, 8).is_some());
    // A relevant Edit grants only the affected read a new comparison.
    let edit = json!({"session_id":"test-session", "hook_event_name":"PostToolUse",
        "tool_name":"Edit", "cwd":workspace(), "tool_input":{"file_path":"status.json"},
        "tool_response":{}});
    assert_eq!(handle(&edit, &c, 8), None);
    assert_eq!(handle(&check, &c, 8), None);
    post["tool_response"]["stdout"] = json!("changed");
    assert_eq!(handle(&post, &c, 8), None);
    assert_eq!(handle(&check, &c, 9), None);
    assert!(handle(&other, &c, 9).is_some());
}

#[test]
fn poll_history_retains_the_newest_256_checks_without_freezing_accounting() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for i in 0..300 {
        assert_eq!(
            handle(&event(&format!("cat state-{i}"), "PreToolUse"), &c, 1),
            None
        );
    }
    let raw: Value = serde_json::from_slice(
        &std::fs::read(c.state_dir.join("status/test-session.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(raw["turn_budget"]["polls"].as_array().unwrap().len(), 256);
    assert!(handle(&event("cat state-299", "PreToolUse"), &c, 2).is_some());
    assert_eq!(handle(&event("cat state-0", "PreToolUse"), &c, 2), None);
}

#[test]
fn workspace_git_mutations_are_allowed_progress_for_local_git_reads_by_design() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let check = event("git status", "PreToolUse");
    assert_eq!(handle(&check, &c, 1), None);
    for command in ["touch marker", "git add unrelated"] {
        assert!(handle(&check, &c, 2).is_some());
        assert_eq!(handle(&event(command, "PostToolUse"), &c, 3), None);
        assert_eq!(handle(&check, &c, 4), None, "{command}");
    }
}

#[path = "turn_budget_poll/comparisons.rs"]
mod comparisons;
