#![cfg(unix)]

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

fn source() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

// Fixture files belong to the temporary repository, not the source checkout.
fn fixture_path(root: &Path, relative: &str) -> PathBuf {
    root.join(relative)
}

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .args([
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
        ])
        .args(args)
        .current_dir(root)
        .output()
        .unwrap();
    assert!(output.status.success(), "{args:?}: {output:?}");
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

fn commit(root: &Path) -> String {
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "fixture\n\nPlan-Item: plangraph:TEST\nWriter: codex/test\nChangelog: skip (isolated test fixture)"]);
    git(root, &["rev-parse", "HEAD"])
}

fn fixture() -> (tempfile::TempDir, String) {
    let dir = tempfile::tempdir().unwrap();
    git(dir.path(), &["init", "-q", "-b", "main"]);
    fs::write(dir.path().join("notes.txt"), "clean\n").unwrap();
    let base = commit(dir.path());
    git(
        dir.path(),
        &["update-ref", "refs/remotes/origin/main", &base],
    );
    git(dir.path(), &["switch", "-c", "topic"]);
    (dir, base)
}

fn hook(root: &Path, heads: &[&str], preflight: bool) -> Output {
    let mut command = Command::new("python3");
    command
        .arg(source().join("tools/hooks/pre-push"))
        .arg("origin")
        .current_dir(root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("HOME", root)
        .env("GIT_DIR", fixture_path(root, ".git"))
        .env("GIT_WORK_TREE", root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if !preflight {
        command.env("PREFLIGHT_SKIP", "1");
    }
    let mut child = command.spawn().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    for head in heads {
        writeln!(
            stdin,
            "refs/heads/topic {head} refs/heads/topic {}",
            "0".repeat(40)
        )
        .unwrap();
    }
    drop(stdin);
    child.wait_with_output().unwrap()
}

#[test]
fn push_other_branch_ignores_checkout_only_oversize_file() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::write(fixture_path(root, "topic.txt"), "clean\n").unwrap();
    let head = commit(root);
    git(root, &["switch", "main"]);
    fs::write(fixture_path(root, "oversize.txt"), "line\n".repeat(801)).unwrap();
    commit(root);
    // Also exercise dirty tracked content in the pushing checkout.
    fs::write(fixture_path(root, "notes.txt"), "dirty\n".repeat(802)).unwrap();
    let output = hook(root, &[&head], false);
    assert!(output.status.success(), "{output:?}");
}

#[test]
fn push_other_branch_refuses_oversize_blob_even_with_checkout_exemption() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::write(fixture_path(root, "oversize.txt"), "line\n".repeat(801)).unwrap();
    let head = commit(root);
    git(root, &["switch", "main"]);
    fs::write(
        fixture_path(root, ".filesize-allow"),
        "oversize.txt checkout only\n",
    )
    .unwrap();
    let output = hook(root, &[&head], false);
    assert!(!output.status.success(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("FAIL oversize.txt: 801"),
        "{output:?}"
    );
}

#[test]
fn push_checks_every_ref_and_uses_pushed_baseline() {
    let (dir, base) = fixture();
    let root = dir.path();
    fs::write(fixture_path(root, "oversize.txt"), "line\n".repeat(801)).unwrap();
    let head = commit(root);
    git(root, &["switch", "main"]);
    fs::write(
        fixture_path(root, ".filesize-baseline"),
        "oversize.txt 801\n",
    )
    .unwrap();
    let output = hook(root, &[&base, &head], false);
    assert!(!output.status.success(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("FAIL oversize.txt: 801"),
        "{output:?}"
    );
}

#[test]
fn push_preflight_runs_at_pushed_head_and_removes_temporary_worktree() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::create_dir(fixture_path(root, "tools")).unwrap();
    fs::write(fixture_path(root, "tools/preflight.py"), "from pathlib import Path\nassert Path('topic.txt').read_text() == 'clean\\n'\nassert not Path('checkout.txt').exists()\nprint('pushed preflight ran')\n").unwrap();
    fs::write(fixture_path(root, "topic.txt"), "clean\n").unwrap();
    let head = commit(root);
    git(root, &["switch", "main"]);
    fs::write(fixture_path(root, "checkout.txt"), "other branch\n").unwrap();
    let before = git(root, &["worktree", "list", "--porcelain"]);
    let output = hook(root, &[&head], true);
    assert!(output.status.success(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("pushed preflight ran"),
        "{output:?}"
    );
    assert_eq!(git(root, &["worktree", "list", "--porcelain"]), before);
    assert!(fixture_path(root, "checkout.txt").exists());
}

#[test]
fn push_preflight_ignores_dirty_files_at_same_head_and_cleans_up_on_failure() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::create_dir(fixture_path(root, "tools")).unwrap();
    fs::write(fixture_path(root, "tools/preflight.py"), "from pathlib import Path\nassert Path('notes.txt').read_text() == 'clean\\n'\nprint('committed test failure')\nraise SystemExit(7)\n").unwrap();
    let head = commit(root);
    fs::write(fixture_path(root, "notes.txt"), "dirty\n").unwrap();
    let before = git(root, &["worktree", "list", "--porcelain"]);
    let output = hook(root, &[&head], true);
    assert_eq!(output.status.code(), Some(7), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("committed test failure"),
        "{output:?}"
    );
    assert_eq!(git(root, &["worktree", "list", "--porcelain"]), before);
    assert_eq!(
        fs::read_to_string(fixture_path(root, "notes.txt")).unwrap(),
        "dirty\n"
    );
}

#[test]
fn push_scrub_reads_pushed_rules_instead_of_dirty_checkout_rules() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::create_dir_all(fixture_path(root, "tools/split")).unwrap();
    fs::write(fixture_path(root, "tools/split/scrub_ci.toml"), "[scrub]\n").unwrap();
    fs::write(fixture_path(root, "topic.txt"), "clean\n").unwrap();
    let clean = commit(root);
    fs::write(
        fixture_path(root, "tools/split/scrub_ci.toml"),
        "invalid toml [\n",
    )
    .unwrap();
    let output = hook(root, &[&clean], false);
    assert!(output.status.success(), "{output:?}");
    let bad = commit(root);
    git(root, &["switch", "main"]);
    let output = hook(root, &[&bad], false);
    assert!(!output.status.success(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("bad scrub config"),
        "{output:?}"
    );
}

#[test]
fn push_file_size_uses_pushed_exemption_when_checkout_has_none() {
    let (dir, _) = fixture();
    let root = dir.path();
    fs::write(fixture_path(root, "oversize.txt"), "line\n".repeat(801)).unwrap();
    fs::write(
        fixture_path(root, ".filesize-allow"),
        "oversize.txt reviewed fixture\n",
    )
    .unwrap();
    let head = commit(root);
    git(root, &["switch", "main"]);
    let output = hook(root, &[&head], false);
    assert!(output.status.success(), "{output:?}");
}
