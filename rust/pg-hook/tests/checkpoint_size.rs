#![cfg(unix)]
use std::path::Path;
use std::process::{Command, Output};

fn git(repo: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

fn fixture() -> (tempfile::TempDir, String) {
    let repo = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    commit(repo.path());
    let base = git(repo.path(), &["rev-parse", "HEAD"]);
    (repo, base)
}

fn commit(repo: &Path) {
    git(repo, &["add", "."]);
    git(
        repo,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "fixture",
            "--allow-empty",
        ],
    );
}

fn check(repo: &Path, base: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_checkpoint-size"))
        .current_dir(repo)
        .args([base, "HEAD"])
        .output()
        .unwrap()
}

#[test]
fn checkpoint_refuses_large_diff_before_seats() {
    let (repo, base) = fixture();
    std::fs::write(repo.path().join("large"), "x".repeat(100_000)).unwrap();
    commit(repo.path());
    let output = check(repo.path(), &base);
    assert_eq!(output.status.code(), Some(1));
    let report = String::from_utf8(output.stdout).unwrap();
    assert!(report.contains("1 files"), "{report}");
    assert!(report.contains("split"), "{report}");
}

#[test]
fn checkpoint_counts_paths_with_newlines_and_binary_files() {
    let (repo, base) = fixture();
    for index in 0..26 {
        std::fs::write(repo.path().join(format!("file\n{index}")), [0, 1, 2]).unwrap();
    }
    commit(repo.path());
    let output = check(repo.path(), &base);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stdout).contains("26 files"));
}

#[test]
fn checkpoint_checks_pr_diff_from_merge_base() {
    let (repo, base) = fixture();
    git(repo.path(), &["checkout", "-qb", "checkpoint"]);
    std::fs::write(repo.path().join("small"), "ok\n").unwrap();
    commit(repo.path());
    let head = git(repo.path(), &["rev-parse", "HEAD"]);
    git(repo.path(), &["checkout", "--detach", &base]);
    std::fs::write(repo.path().join("upstream"), "x".repeat(100_000)).unwrap();
    commit(repo.path());
    let output = Command::new(env!("CARGO_BIN_EXE_checkpoint-size"))
        .current_dir(repo.path())
        .args(["HEAD", &head])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("1 files"));
}

#[test]
fn checkpoint_invalid_base_is_an_error() {
    let (repo, _) = fixture();
    assert_eq!(check(repo.path(), "missing-ref").status.code(), Some(2));
}

#[test]
fn checkpoint_binary_payload_consumes_byte_budget() {
    let (repo, base) = fixture();
    let mut state = 42_u32;
    let payload: Vec<_> = (0..100_000)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            state as u8
        })
        .collect();
    std::fs::write(repo.path().join("binary"), payload).unwrap();
    commit(repo.path());
    assert_eq!(check(repo.path(), &base).status.code(), Some(1));
}

#[test]
fn checkpoint_from_subdirectory_still_measures_whole_pr() {
    let (repo, base) = fixture();
    std::fs::create_dir(repo.path().join("subdir")).unwrap();
    std::fs::write(repo.path().join("large"), "x".repeat(100_000)).unwrap();
    commit(repo.path());
    let output = Command::new(env!("CARGO_BIN_EXE_checkpoint-size"))
        .current_dir(repo.path().join("subdir"))
        .args([&base, "HEAD"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
}
