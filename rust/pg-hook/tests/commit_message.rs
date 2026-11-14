#![cfg(unix)]
use std::path::Path;
use std::process::{Command, Output};

fn git(root: &Path, args: &[&str]) -> Output {
    Command::new("git")
        .current_dir(root)
        .args(args)
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
    let hooks = dir.path().join("hooks");
    std::fs::create_dir(&hooks).unwrap();
    std::fs::copy(env!("CARGO_BIN_EXE_commit-msg"), hooks.join("commit-msg")).unwrap();
    assert!(git(
        dir.path(),
        &["config", "core.hooksPath", hooks.to_str().unwrap()]
    )
    .status
    .success());
    let tools = dir.path().join("tools");
    std::fs::create_dir(&tools).unwrap();
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
