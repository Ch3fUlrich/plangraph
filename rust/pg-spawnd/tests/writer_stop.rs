#![cfg(unix)]

use serde_json::json;
use std::{fs, path::Path, process::Command};

fn git(at: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(at)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

#[test]
fn landed_writer_keeps_a_resume_session_and_unpushed_writer_keeps_running() {
    let tmp = tempfile::tempdir().unwrap();
    let landed_dir = tmp.path().join("landed");
    let unpushed_dir = tmp.path().join("unpushed");
    let work = landed_dir.join("worktree");
    let other = unpushed_dir.join("worktree");
    fs::create_dir_all(&work).unwrap();
    fs::create_dir_all(&other).unwrap();
    git(&work, &["init", "-q", "-b", "main"]);
    git(&work, &["commit", "-q", "--allow-empty", "-m", "landed"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    git(&work, &["branch", "writer"]);
    let remote = tmp.path().join("remote.git");
    git(
        tmp.path(),
        &[
            "clone",
            "-q",
            "--bare",
            work.to_str().unwrap(),
            remote.to_str().unwrap(),
        ],
    );
    git(
        &work,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(
        tmp.path(),
        &[
            "clone",
            "-q",
            remote.to_str().unwrap(),
            other.to_str().unwrap(),
        ],
    );
    git(&other, &["commit", "-q", "--allow-empty", "-m", "unpushed"]);
    let first = landed_dir.join("record.json");
    let second = unpushed_dir.join("record.json");
    for (record, cwd) in [(&first, &work), (&second, &other)] {
        fs::write(
            record,
            json!({"run_id": "run-fixture", "run_kind": "native-codex",
            "status": "completed", "pushed": "refs/heads/writer", "cwd": cwd,
            "session_id": "resume-fixture"})
            .to_string(),
        )
        .unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["writer-stop", &head])
        .args([&first, &second])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report[0]["status"], "stopped");
    assert_eq!(report[0]["session_id"], "resume-fixture");
    assert_eq!(report[1]["status"], "kept");
    assert!(report[1]["reason"].as_str().unwrap().contains("unpushed"));
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(
            &fs::read(landed_dir.join("landing-stop.json")).unwrap()
        )
        .unwrap()["session_id"],
        "resume-fixture"
    );
    assert!(!unpushed_dir.join("landing-stop.json").exists());
}
