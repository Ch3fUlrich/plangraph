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
        for local in [
            "touch marker",
            "cargo build",
            "sed -i s/a/b/ status.json",
            "python x.py",
        ] {
            assert_eq!(handle(&event(local, "PostToolUse"), &c, 5), None);
            assert!(
                handle(&pre, &c, 6).is_some(),
                "local work: {command}: {local}"
            );
        }
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
fn ordinary_substitutions_and_variable_indirection_are_not_guessed_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "git status $(date)",
        "git status `date`",
        "echo \"$(cat state)\"",
        "tool=git; $tool status",
        "$tool status",
        "git $subcommand",
        "cat \"$status\"",
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

#[test]
fn dollar_leading_commands_only_count_as_poll_checks_inside_loops() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for command in [
        "sleep 5; $SCRIPT",
        "sleep 5; MODE=x $RUNNER",
        "sleep 5; env MODE=x $RUNNER",
        "sleep 5; bash -c '$SCRIPT'",
    ] {
        for now in 1..=2 {
            assert_eq!(
                handle(&event(command, "PreToolUse"), &c, now),
                None,
                "{command}"
            );
        }
    }
    for command in [
        "while true; do $SCRIPT; done",
        "while true; do MODE=x $RUNNER; done",
        "while true; do bash -c '$SCRIPT'; done",
    ] {
        assert!(
            handle(&event(command, "PreToolUse"), &c, 3).is_some(),
            "{command}"
        );
    }
}

#[test]
fn polling_scan_depth_cap_is_an_explicit_fail_open_boundary() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for depth in 0..=4 {
        let mut body = "sleep 2; git status".to_owned();
        for _ in 0..depth {
            body = format!("echo \"$({body})\"");
        }
        let command = format!("while true; do {body}; done");
        assert_eq!(
            handle(&event(&command, "PreToolUse"), &c, 1).is_some(),
            depth <= 3,
            "substitution depth {depth}: {command}"
        );
        let mut command = "while true; do git status; done".to_owned();
        for _ in 0..depth {
            command = format!("bash -c '{}'", command.replace('\'', "'\\''"));
        }
        assert_eq!(
            handle(&event(&command, "PreToolUse"), &c, 1).is_some(),
            depth <= 3,
            "shell depth {depth}: {command}"
        );
        let mut body = "git status $(echo)".to_owned();
        for _ in 0..depth {
            body = format!("bash -c '{}'", body.replace('\'', "'\\''"));
        }
        let command = format!("while true; do {body}; done");
        assert_eq!(
            handle(&event(&command, "PreToolUse"), &c, 1).is_some(),
            depth <= 3,
            "snapshot-command depth {depth}: {command}"
        );
    }
}

#[test]
fn unsupported_read_syntax_cannot_hide_sleep_or_snapshot_loops() {
    for command in [
        "sleep 5; git status $(true)",
        "sleep 5; git status `true`",
        "while true; do sleep 2; git status $(echo); done",
        "while true; do sleep 2; git status `echo`; done",
        "while true; do sleep 2; git status; done </dev/null",
        "while true; do sleep 2; git status >status; done",
        "while true; do sleep 2; git status; done; echo '",
        "while true; do git status $(echo); done",
        "while true; do git status `echo`; done",
        "while true; do git status; done </dev/null",
        "while true; do git status >status; done",
        "while true; do <status.json git status; done",
        "while true; do >status.json git status; done",
        "while true; do 2>/dev/null git status; done",
        "<status.json while true; do git status; done",
        "while true; do git status; done; echo '",
        "echo '; while true; do sleep 2; git status; done",
        "echo \"$(while true; do sleep 2; git status; done)\"",
        "tool=git; while true; do $tool status; done",
        "tool=git; while true; do ${tool} status; done",
        "while true; do tool=git; $tool status; done",
        "while true; do MODE=plain git status; done",
        "while true; do git status; done; echo ok \\",
        "cat <<EOF; while true; do sleep 2; git status; done\nEOF",
        "while true; do git $subcommand; done",
        "tool=git; while true; do sleep 2; $tool status; done",
        "bash -c 'while true; do sleep 2; git status $(echo); done'",
        "bash -c 'while true; do git status $(echo); done'",
        "while true; do bash -c 'git status $(echo)'; done",
        "while true; do bash -c 'git status'; done </dev/null",
        "sleep 2; git status $(echo unterminated",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        let line = handle(&event(command, "PreToolUse"), &c, 1)
            .unwrap_or_else(|| panic!("poll escaped: {command}"));
        assert!(line.contains("python -m plangraph.spawner.cli wait <run_id>"));
    }
}

#[test]
fn literal_path_spellings_share_history_without_normalising_search_patterns() {
    for (first, second) in [
        ("cat ./status.json", "cat status.json"),
        ("head ./dir/./status.json", "head dir/status.json"),
        ("stat './status.json'", "stat status.json"),
        ("rg ready ./status.json", "rg ready status.json"),
        ("git diff -- ./status.json", "git diff -- status.json"),
        ("git diff ./status.json", "git diff status.json"),
        ("rg -e ready ./status.json", "rg -e ready status.json"),
        ("cat -- ./status.json", "cat -- status.json"),
        ("cat -n ./status.json", "cat -n status.json"),
        ("rg -n ready ./status.json", "rg -n ready status.json"),
        ("git status ./status.json", "git status status.json"),
        ("ls -t ./status.json", "ls -t status.json"),
        ("cat -t ./status.json", "cat -t status.json"),
        ("test -f ./status.json", "test -f status.json"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        assert_eq!(handle(&event(first, "PreToolUse"), &c, 1), None);
        assert!(
            handle(&event(second, "PreToolUse"), &c, 2).is_some(),
            "{first}"
        );
    }
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    for (first, second) in [
        ("rg ./ready status.json", "rg ready status.json"),
        ("rg -e ./ready status.json", "rg -e ready status.json"),
        (
            "rg --glob ./ready pattern status.json",
            "rg --glob ready pattern status.json",
        ),
        ("test ./ready = ready", "test ready = ready"),
        ("stat -c ./ready status.json", "stat -c ready status.json"),
        (
            "ls --ignore ./ready status.json",
            "ls --ignore ready status.json",
        ),
    ] {
        assert_eq!(handle(&event(first, "PreToolUse"), &c, 1), None);
        assert_eq!(handle(&event(second, "PreToolUse"), &c, 1), None);
    }
}

#[test]
fn successful_non_read_commands_grant_exactly_one_local_retry() {
    for command in [
        "cargo build",
        "sed -i s/a/b/ status.json",
        "python x.py",
        "echo ok",
    ] {
        for read in ["git status", "cat status.json"] {
            let dir = tempfile::tempdir().unwrap();
            let c = config(&dir);
            let pre = event(read, "PreToolUse");
            assert_eq!(handle(&pre, &c, 1), None);
            assert!(handle(&pre, &c, 2).is_some());
            assert_eq!(handle(&event(command, "PreToolUse"), &c, 3), None);
            assert!(handle(&pre, &c, 4).is_some(), "pre is not success");
            let mut failed = event(command, "PostToolUse");
            failed["tool_response"]["exit_code"] = json!(1);
            assert_eq!(handle(&failed, &c, 5), None);
            assert!(handle(&pre, &c, 6).is_some(), "failed is not success");
            assert_eq!(handle(&event(command, "PostToolUse"), &c, 7), None);
            assert_eq!(handle(&pre, &c, 8), None, "{read}: {command}");
            let line = handle(&pre, &c, 9).unwrap();
            assert!(line.contains("successful non-read command"), "{line}");
            assert!(line.contains("retry once"), "{line}");
        }
    }
}

#[test]
fn next_pre_expires_an_admission_whose_post_never_arrived() {
    for intervening in ["cat status.json", "cat other.json", "cargo build"] {
        let dir = tempfile::tempdir().unwrap();
        let c = config(&dir);
        let pre = event("cat status.json", "PreToolUse");
        assert_eq!(handle(&pre, &c, 1), None);
        let _ = handle(&event(intervening, "PreToolUse"), &c, 2);
        let mut late = event("cat status.json", "PostToolUse");
        late["tool_response"]["exit_code"] = json!(1);
        assert_eq!(handle(&late, &c, 3), None);
        assert!(handle(&pre, &c, 4).is_some(), "{intervening}");
    }
}

#[test]
fn script_file_contents_are_an_explicit_uninspected_boundary() {
    let dir = tempfile::tempdir().unwrap();
    let c = config(&dir);
    std::fs::write(
        dir.path().join("poll.sh"),
        "while true; do sleep 2; git status; done\n",
    )
    .unwrap();
    for command in ["bash poll.sh", "./poll.sh"] {
        let mut pre = event(command, "PreToolUse");
        pre["cwd"] = json!(dir.path());
        for now in 1..=2 {
            assert_eq!(handle(&pre, &c, now), None, "{command}");
        }
    }
}
