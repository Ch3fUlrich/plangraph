#![cfg(unix)]
use std::path::Path;
use std::process::{Command, Output};

fn git(root: &Path, args: &[&str]) -> Output {
    Command::new("git")
        .current_dir(root)
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
        let output = Command::new("git")
            .current_dir(dir.path())
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
        assert!(output.status.success(), "{output:?}");
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
        let output = Command::new("git")
            .current_dir(dir.path())
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
