//! Exercise the shared admission gate through the public Rust command.
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn git(repo: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", repo.join("absent-config"))
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{:?}: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

// Plumbing creates isolated fixture history without hooks, signing or index edits.
fn commit(repo: &Path, message: &str, parent: Option<&str>) {
    let tree = git(repo, &["mktree"]);
    let mut args = vec!["commit-tree", &tree, "-m", message];
    if let Some(parent) = parent {
        args.extend(["-p", parent]);
    }
    let sha = git(repo, &args);
    git(repo, &["update-ref", "refs/heads/main", &sha]);
}

fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
    let root = tempfile::tempdir().unwrap();
    let origin = root.path().join("origin");
    let repo = root.path().join("checkout");
    std::fs::create_dir(&origin).unwrap();
    git(&origin, &["init", "--initial-branch=main"]);
    commit(&origin, "initial", None);
    git(
        root.path(),
        &["clone", origin.to_str().unwrap(), repo.to_str().unwrap()],
    );
    (root, origin, repo)
}

fn check(repo: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env(
            "PYTHONPATH",
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src"),
        )
        // This command must not require loading setup's environment file.
        .env("PLANGRAPH_ENV_FILE", repo.join("missing.env"))
        .args(["base-fresh", repo.to_str().unwrap()])
        .output()
        .unwrap()
}

#[test]
fn base_fresh_refuses_stale_checkout_before_any_launch_artifacts() {
    let (root, origin, repo) = fixture();
    let before = git(&repo, &["rev-parse", "HEAD"]);
    let parent = git(&origin, &["rev-parse", "main"]);
    commit(&origin, "advance", Some(&parent));
    let output = check(&repo);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        "base_stale: behind=1\n"
    );
    assert!(output.stdout.is_empty());
    assert_eq!(git(&repo, &["rev-parse", "HEAD"]), before);
    assert_eq!(
        git(&repo, &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );
    assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 2);
    assert_eq!(std::fs::read_dir(&repo).unwrap().count(), 1); // only .git
}

#[test]
fn base_fresh_accepts_current_checkout_and_fetches_new_origin() {
    let (_root, origin, repo) = fixture();
    let output = check(&repo);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8(output.stdout).unwrap(), "base_current\n");
    assert!(output.stderr.is_empty());
    let parent = git(&origin, &["rev-parse", "main"]);
    commit(&origin, "advance", Some(&parent));
    assert!(!check(&repo).status.success());
    git(&repo, &["merge", "--ff-only", "origin/main"]);
    assert!(check(&repo).status.success());
}

#[test]
fn base_fresh_forwards_fetch_failure_and_git_failure_as_one_reason() {
    let (root, _origin, repo) = fixture();
    git(
        &repo,
        &[
            "remote",
            "set-url",
            "origin",
            root.path().join("missing-origin").to_str().unwrap(),
        ],
    );
    let output = check(&repo);
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        "base_stale: fetch_failed\n"
    );
    let output = check(&root.path().join("missing-repo"));
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        "base_stale: git_failed\n"
    );
}

#[test]
fn base_fresh_fails_closed_when_shared_gate_cannot_start() {
    let root = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env("PG_SPAWND_PYTHON", root.path().join("missing-python"))
        .args(["base-fresh", root.path().to_str().unwrap()])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        "base_stale: gate_unavailable\n"
    );
}
