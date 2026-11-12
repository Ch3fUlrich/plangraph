use super::*;

#[test]
fn common_wrappers_share_read_history_and_relevant_mutation_scope() {
    for command in [
        "timeout 2 git status",
        "timeout -s TERM -k 1 2 git status",
        "env git status",
        "env -i MODE=test git status",
        "env -u MODE git status",
        "nice git status",
        "nice -n 5 git status",
        "nohup git status",
        "command git status",
        "command -p git status",
        "bash -c 'git status'",
        "sh -lc 'git status'",
        "env timeout 2 bash -c 'git status'",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        let check = event(command, "PreToolUse");
        assert_eq!(handle(&check, &c, 1), None, "{command}");
        assert!(handle(&check, &c, 2).is_some(), "{command}");
        assert!(
            handle(&event("git status", "PreToolUse"), &c, 3).is_some(),
            "{command}"
        );
        assert_eq!(handle(&event("touch marker", "PostToolUse"), &c, 4), None);
        assert_eq!(handle(&check, &c, 5), None, "{command}");
        assert!(handle(&check, &c, 6).is_some(), "{command}");
    }
}

#[test]
fn remote_comparison_runs_use_real_pre_post_order_and_a_bounded_window() {
    for command in [
        "gh pr checks 42",
        "python -m plangraph.spawner.cli status fake-run",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        let pre = event(command, "PreToolUse");
        let mut post = event(command, "PostToolUse");
        for (now, output) in [(1, "pending"), (2, "running"), (3, "running")] {
            assert_eq!(handle(&pre, &c, now), None, "comparison: {command}");
            post["tool_response"]["stdout"] = json!(output);
            assert_eq!(handle(&post, &c, now), None);
        }
        assert!(handle(&pre, &c, 4).is_some(), "unchanged: {command}");
        assert_eq!(handle(&event("touch marker", "PostToolUse"), &c, 5), None);
        assert!(handle(&pre, &c, 6).is_some(), "local writes: {command}");
        assert!(handle(&pre, &c, 62).is_some(), "inside window: {command}");
        assert_eq!(handle(&pre, &c, 63), None, "cooldown: {command}");
        post["tool_response"]["stdout"] = json!("complete");
        assert_eq!(handle(&post, &c, 63), None);
        assert_eq!(handle(&pre, &c, 64), None);
        assert_eq!(handle(&post, &c, 64), None);
        assert!(handle(&pre, &c, 65).is_some());
    }
}

#[test]
fn remote_reads_without_comparable_output_never_claim_an_identical_result() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let pre = event("gh pr checks 42", "PreToolUse");
    let post = event("gh pr checks 42", "PostToolUse");
    for now in 1..=3 {
        assert_eq!(handle(&pre, &c, now), None);
        assert_eq!(handle(&post, &c, now), None);
    }
    assert_eq!(handle(&pre, &c, 4), None);
    assert_eq!(
        handle(&pre, &c, 5),
        None,
        "missing post is not identical output"
    );
}

#[test]
fn legacy_remote_history_can_reach_a_comparison_after_upgrade() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let pre = event("gh pr checks 42", "PreToolUse");
    assert_eq!(handle(&pre, &c, 1), None);
    let path = c.state_dir.join("status/test-session.json");
    let mut document: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    for read in document["turn_budget"]["reads"]
        .as_object_mut()
        .unwrap()
        .values_mut()
    {
        for field in ["remote", "pending", "compared_at"] {
            read.as_object_mut().unwrap().remove(field);
        }
    }
    std::fs::write(&path, document.to_string()).unwrap();
    assert_eq!(handle(&pre, &c, 2), None);
}

#[test]
fn unmatched_read_posts_cannot_manufacture_progress() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    let pre = event("cat status.json", "PreToolUse");
    let mut post = event("cat status.json", "PostToolUse");
    post["tool_response"]["stdout"] = json!("baseline");
    assert_eq!(handle(&pre, &c, 1), None);
    assert_eq!(handle(&post, &c, 1), None);
    assert!(handle(&pre, &c, 2).is_some());
    post["tool_response"]["stdout"] = json!("fabricated progress");
    assert_eq!(handle(&post, &c, 3), None);
    assert!(handle(&pre, &c, 4).is_some());
}

#[test]
fn failed_reads_allow_corrected_and_identical_retries() {
    for command in ["cat missing.json", "gh pr checks 42"] {
        for failure in [
            json!({"exit_code":1}),
            json!({"is_error":true}),
            json!({"interrupted":true}),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let c = config(&dir);
            let pre = event(command, "PreToolUse");
            let mut post = event(command, "PostToolUse");
            for now in 1..=2 {
                assert_eq!(handle(&pre, &c, now), None, "{command}: {failure}");
                post["tool_response"] = failure.clone();
                assert_eq!(handle(&post, &c, now), None);
            }
            assert_eq!(handle(&event("cat real.json", "PreToolUse"), &c, 3), None);
            assert_eq!(handle(&pre, &c, 3), None);
        }
    }
}

#[test]
fn substitutions_and_variable_indirection_are_allowed_by_design_not_a_shell_sandbox() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "while true; do sleep 2; git status $(echo); done",
        "git status $(date)",
        "git status `date`",
        "echo \"$(cat state)\"",
        "tool=git; $tool status",
        "$tool status",
        "git $subcommand",
        "cat \"$status\"",
        "tool=git; while true; do $tool status; done",
    ] {
        for _ in 0..2 {
            assert_eq!(
                handle(&event(command, "PreToolUse"), &c, 1),
                None,
                "{command}"
            );
        }
    }
}
