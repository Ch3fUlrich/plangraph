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

#[test]
fn installing_and_removing_a_lane_preserves_other_worktrees_hooks() {
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    assert!(git(root, &["init", "-b", "main"]).status.success());
    std::fs::create_dir_all(root.join("tools/hooks")).unwrap();
    for path in ["tools/ci_commit_gate.py", "tools/hooks/pre-push"] {
        std::fs::copy(source.join(path), root.join(path)).unwrap();
    }
    assert!(git(root, &["add", "."]).status.success());
    assert!(git(
        root,
        &["-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture"]
    )
    .status
    .success());
    assert!(git(
        root,
        &["remote", "add", "origin", "https://example.invalid/public"]
    )
    .status
    .success());
    assert!(git(root, &["config", "core.hooksPath", "tools/hooks"])
        .status
        .success());
    let lane = tempfile::tempdir().unwrap();
    assert!(git(
        root,
        &["worktree", "add", "--detach", lane.path().to_str().unwrap()]
    )
    .status
    .success());
    let install = Command::new(env!("CARGO_BIN_EXE_commit-msg"))
        .arg("--install")
        .arg(lane.path())
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(install.status.success(), "{install:?}");
    assert_eq!(
        String::from_utf8_lossy(
            &git(root, &["config", "--local", "--get", "core.hooksPath"]).stdout
        )
        .trim(),
        "tools/hooks"
    );
    let hooks = lane.path().join("tools/hooks");
    assert_eq!(
        String::from_utf8_lossy(&git(lane.path(), &["config", "--get", "core.hooksPath"]).stdout)
            .trim(),
        hooks.to_str().unwrap()
    );
    let push_install = Command::new("python3")
        .arg(source.join("tools/check_push_gate.py"))
        .arg("--install")
        .arg(lane.path())
        .env("PLANGRAPH_PUBLIC_REMOTES", "example.invalid")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(push_install.status.success(), "{push_install:?}");
    assert!(hooks.join("commit-msg").is_file());
    let rejected = git(lane.path(), &["commit", "--allow-empty", "-m", "x"]);
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("missing Writer trailer"));
    assert!(git(
        root,
        &[
            "worktree",
            "remove",
            "--force",
            lane.path().to_str().unwrap()
        ]
    )
    .status
    .success());
    assert_eq!(
        String::from_utf8_lossy(&git(root, &["config", "--get", "core.hooksPath"]).stdout).trim(),
        "tools/hooks"
    );
    assert!(root.join("tools/hooks/pre-push").is_file());
}
