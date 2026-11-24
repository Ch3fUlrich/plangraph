#![cfg(unix)]
use std::path::Path;
use std::process::Output;

fn git(root: &Path, args: &[&str]) -> Output {
    pg_worker_core::git_test_support::command(root)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap()
}

fn repo() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    assert!(git(dir.path(), &["init", "-b", "main"]).status.success());
    let tools = dir.path().join("tools");
    std::fs::create_dir(&tools).unwrap();
    let hooks = tools.join("hooks");
    std::fs::create_dir(&hooks).unwrap();
    std::fs::copy(env!("CARGO_BIN_EXE_commit-msg"), hooks.join("commit-msg")).unwrap();
    assert!(git(
        dir.path(),
        &["config", "core.hooksPath", hooks.to_str().unwrap()]
    )
    .status
    .success());
    std::fs::copy(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/ci_commit_gate.py"),
        tools.join("ci_commit_gate.py"),
    )
    .unwrap();
    dir
}

#[test]
fn real_commits_share_the_ci_trailer_rules() {
    let dir = repo();
    for (message, diagnostic) in [
        ("x", "missing Writer trailer"),
        (
            "x\n\nWriter: codex\n# retained body",
            "missing Writer trailer",
        ),
        ("x\n\nWriter: ", "malformed Writer trailer"),
        ("x\n\nHand-edit: reason", "malformed Hand-Edit trailer"),
        ("x\n\nWriter: codex\nChangelog: skip", "malformed Changelog"),
        (
            "x\n\nWriter: codex\nChangelog: skip ( )",
            "malformed Changelog",
        ),
        (
            "x\n\nWriter: codex\nChangelog: skip ((nested))",
            "malformed Changelog",
        ),
        ("x\n\nWriter: codex\n\nbody", "missing Writer trailer"),
    ] {
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
        assert!(!output.status.success(), "{message}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(diagnostic),
            "{output:?}"
        );
    }
    for message in [
        "x\n\nWriter: codex",
        "x\n\nHand-Edit: typo",
        "x\n\nWriter: codex\nChangelog: skip (test fixture)",
    ] {
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
        assert!(output.status.success(), "{output:?}");
    }
}

#[test]
fn actual_merge_commit_needs_no_trailers() {
    let dir = repo();
    for args in [
        vec!["commit", "--allow-empty", "-m", "base\n\nWriter: codex"],
        vec!["checkout", "-b", "other"],
        vec!["commit", "--allow-empty", "-m", "other\n\nWriter: codex"],
        vec!["checkout", "main"],
        vec!["merge", "--no-ff", "other", "-m", "merge without trailers"],
    ] {
        let output = git(dir.path(), &args);
        assert!(output.status.success(), "{output:?}");
    }
    assert_eq!(
        String::from_utf8_lossy(&git(dir.path(), &["rev-list", "--parents", "-1", "HEAD"]).stdout)
            .split_whitespace()
            .count(),
        3
    );
}

#[test]
fn missing_policy_fails_closed() {
    let dir = repo();
    std::fs::remove_file(dir.path().join("tools/ci_commit_gate.py")).unwrap();
    assert!(!git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "x\n\nWriter: codex"]
    )
    .status
    .success());
}

#[test]
fn editor_comments_do_not_hide_valid_trailers() {
    let dir = repo();
    for (key, comment_char) in [
        ("core.commentChar", "#"),
        ("core.commentChar", ";"),
        ("core.commentChar", "auto"),
        ("core.commentString", "//"),
    ] {
        assert!(git(dir.path(), &["config", key, comment_char])
            .status
            .success());
        std::fs::write(dir.path().join("staged.txt"), comment_char).unwrap();
        assert!(git(dir.path(), &["add", "staged.txt"]).status.success());
        let output = pg_worker_core::git_test_support::command(dir.path())
            .args([
                "commit",
                "--allow-empty",
                "--edit",
                "--verbose",
                "-m",
                "editor commit\n\n# body forces auto to choose another prefix\n\nWriter: codex",
            ])
            .env("GIT_EDITOR", "true")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert!(output.status.success(), "{key}={comment_char}: {output:?}");
    }
}

#[test]
fn installed_validator_works_in_an_older_linked_worktree() {
    let dir = repo();
    assert!(git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "base\n\nWriter: codex"]
    )
    .status
    .success());
    let linked = tempfile::tempdir().unwrap();
    assert!(git(
        dir.path(),
        &[
            "worktree",
            "add",
            "--detach",
            linked.path().to_str().unwrap()
        ]
    )
    .status
    .success());
    // No gate script exists in this older worktree. The installed pair owns policy.
    assert!(!linked.path().join("tools/ci_commit_gate.py").exists());
    let output = git(
        linked.path(),
        &["commit", "--allow-empty", "-m", "x\n\nWriter: codex"],
    );
    assert!(output.status.success(), "{output:?}");
    let output = git(linked.path(), &["commit", "--allow-empty", "-m", "x"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("missing Writer trailer"));
}

#[test]
fn a_new_commit_after_a_merge_still_requires_attribution() {
    let dir = repo();
    for args in [
        vec!["commit", "--allow-empty", "-m", "base\n\nWriter: codex"],
        vec!["checkout", "-b", "other"],
        vec!["commit", "--allow-empty", "-m", "other\n\nWriter: codex"],
        vec!["checkout", "main"],
        vec!["merge", "--no-ff", "other", "-m", "merge without trailers"],
    ] {
        assert!(git(dir.path(), &args).status.success());
    }
    let output = git(
        dir.path(),
        &["commit", "--allow-empty", "-m", "merge without trailers"],
    );
    assert!(!output.status.success());
    let output = git(
        dir.path(),
        &["commit", "--allow-empty", "--amend", "--no-edit"],
    );
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("missing Writer trailer"));
}

#[test]
fn automatic_comments_without_scissors_match_the_final_message() {
    let dir = repo();
    assert!(git(dir.path(), &["config", "core.commentChar", "auto"])
        .status
        .success());
    for (message, valid) in [
        ("subject\n\n# body\n\nWriter: codex", true),
        (
            "subject\n\n# leading body\n\nWriter: codex\n\n# retained body",
            false,
        ),
    ] {
        let output = pg_worker_core::git_test_support::command(dir.path())
            .args(["commit", "--allow-empty", "--edit", "-m", message])
            .env("GIT_EDITOR", "true")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert_eq!(output.status.success(), valid, "{output:?}");
    }
}

#[test]
fn attribution_paragraphs_and_misplaced_known_trailers() {
    let dir = repo();
    let message = "fix typo\n\nExplain the correction.\n\nPlan-Item: plangraph:TEST\nHand-Edit: typo correction\nChangelog: skip (typo correction)\n\nCo-Authored-By: Claude <claude@example.invalid>\nClaude-Session: session_test";
    let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
    assert!(output.status.success(), "{output:?}");
    for key in ["Plan-Item", "Writer", "Hand-Edit", "Changelog", "Gate-Edit"] {
        let value = match key {
            "Changelog" => "skip (typo correction)",
            "Gate-Edit" => "D-1234",
            _ => "codex",
        };
        for separator in ["\n", "\n\n"] {
            let message =
                format!("subject\n\n{key}: {value}{separator}Prose follows.\n\nWriter: codex");
            let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
            assert!(!output.status.success(), "{message}: {output:?}");
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(stderr.contains(&format!("line 3: {key}")), "{stderr}");
            assert!(
                stderr.contains("outside trailing trailer block"),
                "{stderr}"
            );
        }
    }
}

#[test]
fn a_malformed_class_search_line_is_refused_with_the_expected_form() {
    let dir = repo();
    for line in [
        "Class-Search: fixture -> 1 hit, 1 changed (prose after the count)",
        "Class-Search: fixture -> 1 hits, 2 changed",
        "Class-Search: fixture -> some hits",
    ] {
        let message = format!("x\n\n{line}\n\nWriter: codex");
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
        assert!(!output.status.success(), "{line}: {output:?}");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("line 3: malformed Class-Search")
                && stderr.contains("expected `Class-Search: <pattern> -> <n> hits, <m> changed`"),
            "{stderr}"
        );
    }
    for line in [
        "Class-Search: fixture -> 1 hit, 1 changed",
        "Class-Search: 0123abcd fixture -> 2 hits, 1 changed",
    ] {
        let message = format!("x\n\n{line}\n\nWriter: codex");
        let output = git(dir.path(), &["commit", "--allow-empty", "-m", &message]);
        assert!(output.status.success(), "{line}: {output:?}");
    }
}

#[test]
fn class_search_is_checked_on_a_merge_and_on_a_verbatim_message() {
    let dir = repo();
    let root = dir.path();
    assert!(git(
        root,
        &["commit", "--allow-empty", "-m", "base\n\nWriter: codex"]
    )
    .status
    .success());
    assert!(git(root, &["checkout", "-qb", "side"]).status.success());
    assert!(git(
        root,
        &["commit", "--allow-empty", "-m", "side\n\nWriter: codex"]
    )
    .status
    .success());
    assert!(git(root, &["checkout", "-q", "main"]).status.success());
    assert!(git(
        root,
        &["commit", "--allow-empty", "-m", "main\n\nWriter: codex"]
    )
    .status
    .success());
    // A merge needs no trailers, but a malformed Class-Search line in it is refused.
    let prose = "Merge side\n\nClass-Search: fixture -> 1 hit, 1 changed (prose)";
    let merge = git(root, &["merge", "--no-ff", "-m", prose, "side"]);
    assert!(!merge.status.success(), "{merge:?}");
    let stderr = String::from_utf8_lossy(&merge.stderr);
    assert!(
        stderr.contains("line 3: malformed Class-Search"),
        "{stderr}"
    );
    assert!(git(root, &["merge", "--abort"]).status.success());
    let merge = git(root, &["merge", "--no-ff", "-m", "Merge side", "side"]);
    assert!(merge.status.success(), "{merge:?}");
    // Verbatim cleanup keeps trailing whitespace, which the range check refuses.
    let message = "x\n\nClass-Search: fixture -> 1 hit, 1 changed \n\nWriter: codex";
    let output = git(
        root,
        &[
            "commit",
            "--allow-empty",
            "--cleanup=verbatim",
            "-m",
            message,
        ],
    );
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("malformed Class-Search"));
}

#[test]
fn a_literal_scissors_look_alike_does_not_hide_a_class_search_line() {
    let dir = repo();
    let marker = "x ------------------------ >8 ------------------------";
    // Only a comment-prefixed scissors line is cut; a look-alike is kept by --cleanup=verbatim.
    let message = format!(
        "x\n\n{marker}\nClass-Search: fixture -> 1 hit, 1 changed (prose)\n\nWriter: codex"
    );
    let output = git(
        dir.path(),
        &[
            "commit",
            "--allow-empty",
            "--cleanup=verbatim",
            "-m",
            &message,
        ],
    );
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("line 4: malformed Class-Search"));
    // `-m` keeps a line below a scissors line even under commit.cleanup=scissors, so it is refused too
    // and no commit is made.
    let before = git(dir.path(), &["rev-list", "--all", "--count"]);
    let message = "x\n\nWriter: codex\n# ------------------------ >8 ------------------------\nClass-Search: fixture -> 1 hit, 1 changed (prose)\n";
    assert!(git(dir.path(), &["config", "commit.cleanup", "scissors"])
        .status
        .success());
    let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("line 5: malformed Class-Search"));
    assert_eq!(
        git(dir.path(), &["rev-list", "--all", "--count"]).stdout,
        before.stdout,
        "a refused commit left a commit"
    );
    // A well-formed line commits, and the saved body keeps it.
    let message = "x\n\nClass-Search: fixture -> 1 hit, 1 changed\n\nWriter: codex";
    let output = git(dir.path(), &["commit", "--allow-empty", "-m", message]);
    assert!(output.status.success(), "{output:?}");
    let body = git(dir.path(), &["log", "-1", "--format=%B"]);
    assert!(
        String::from_utf8_lossy(&body.stdout).contains("Class-Search: fixture -> 1 hit, 1 changed")
    );
}
