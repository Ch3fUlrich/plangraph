use std::process::Command;

fn bin() -> Command {
    Command::new(env!("CARGO_BIN_EXE_pg-session-resume"))
}

#[test]
fn tick_over_empty_directories_succeeds_and_unknown_commands_fail() {
    let dir = tempfile::tempdir().unwrap();
    let ok = bin()
        .args(["tick", "--state"])
        .arg(dir.path())
        .arg("--pr-dir")
        .arg(dir.path())
        .output()
        .unwrap();
    assert!(
        ok.status.success(),
        "{}",
        String::from_utf8_lossy(&ok.stderr)
    );
    let bad = bin()
        .args(["bogus", "--state"])
        .arg(dir.path())
        .output()
        .unwrap();
    assert_eq!(bad.status.code(), Some(2));
    let bad = bin().args(["tick", "--idle-min", "x"]).output().unwrap();
    assert_eq!(bad.status.code(), Some(2));
}

/// A state dir with one DONE writer whose Claude job state file reads `job_state`.
fn done_writer(job_state: &str) -> (tempfile::TempDir, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let (launch, pr) = (dir.path().join("launch/sessions"), dir.path().join("pr"));
    std::fs::create_dir_all(&launch).unwrap();
    std::fs::create_dir_all(&pr).unwrap();
    let sid = "1591af3e-91d1-442a-bc81-f5dd71769181";
    std::fs::write(
        launch.join("w-1.json"),
        format!(
            r#"{{"name":"w","session_name":"w-1","cwd":"/x/lane-a","session_id":"{sid}","ts":1}}"#
        ),
    )
    .unwrap();
    std::fs::write(
        pr.join("lane-a.status"),
        "lane-a DONE\nHEAD: 0123456789abcdef0123456789abcdef01234567\n",
    )
    .unwrap();
    // a clean HOME of its own: the host's real jobs never decide this test
    let home = tempfile::tempdir().unwrap();
    let job = home.path().join(".claude/jobs/1591af3e");
    std::fs::create_dir_all(&job).unwrap();
    std::fs::write(job.join("state.json"), job_state).unwrap();
    (dir, home)
}

fn stop_done(dir: &tempfile::TempDir, home: &tempfile::TempDir) -> std::process::Output {
    bin()
        .args(["stop-done", "--quiet-s", "0", "--state"])
        .arg(dir.path())
        .arg("--pr-dir")
        .arg(dir.path().join("pr"))
        .env("PATH", "/nonexistent")
        .env("HOME", home.path())
        .output()
        .unwrap()
}

#[test]
fn done_status_with_a_missing_claude_binary_logs_a_failed_stop() {
    let (dir, home) = done_writer(r#"{"state":"done","inFlight":{"tasks":0}}"#);
    let out = stop_done(&dir, &home);
    assert!(out.status.success());
    let log = std::fs::read_to_string(dir.path().join("session-lifecycle.log")).unwrap();
    assert!(
        log.contains("event=stop-failed name=w-1 reason=done"),
        "{log}"
    );
}

#[test]
fn a_job_state_with_a_garbled_task_count_is_kept() {
    let (dir, home) = done_writer(r#"{"state":"done","inFlight":{"tasks":"2"}}"#);
    for _ in 0..2 {
        assert!(stop_done(&dir, &home).status.success());
    }
    let log = std::fs::read_to_string(dir.path().join("session-lifecycle.log")).unwrap();
    assert_eq!(log.matches("why=job-tasks-unknown").count(), 1, "{log}");
    assert!(!log.contains("event=stop-failed"), "{log}");
}

#[test]
fn a_stop_that_cannot_be_recorded_fails_the_run() {
    let (dir, home) = done_writer(r#"{"state":"stopped"}"#);
    // the temp file of the record's atomic rename cannot be created
    std::fs::create_dir(dir.path().join("launch/sessions/w-1.json.tmp")).unwrap();
    let out = stop_done(&dir, &home);
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("could not be written"));
}

#[test]
fn a_log_line_that_cannot_be_persisted_fails_the_run() {
    let dir = tempfile::tempdir().unwrap();
    let (launch, pr) = (dir.path().join("launch/sessions"), dir.path().join("pr"));
    std::fs::create_dir_all(&launch).unwrap();
    std::fs::create_dir_all(&pr).unwrap();
    std::fs::create_dir(dir.path().join("session-lifecycle.log")).unwrap(); // appending to a directory fails
    let sid = "1591af3e-91d1-442a-bc81-f5dd71769181";
    std::fs::write(
        launch.join("w-1.json"),
        format!(
            r#"{{"name":"w","session_name":"w-1","cwd":"/x/lane-a","session_id":"{sid}","ts":1}}"#
        ),
    )
    .unwrap();
    std::fs::write(
        pr.join("lane-a.status"),
        "lane-a DONE\nHEAD: 0123456789abcdef0123456789abcdef01234567\n",
    )
    .unwrap();
    let home = tempfile::tempdir().unwrap();
    let jobs = home.path().join(".claude/jobs/1591af3e");
    std::fs::create_dir_all(&jobs).unwrap();
    std::fs::write(jobs.join("state.json"), r#"{"state":"done"}"#).unwrap();
    let out = bin()
        .args(["stop-done", "--quiet-s", "0", "--state"])
        .arg(dir.path())
        .arg("--pr-dir")
        .arg(&pr)
        .env("PATH", "/nonexistent")
        .env("HOME", home.path())
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(2),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stderr).contains("cannot append"));
}

#[test]
fn a_tick_that_finds_the_lock_held_does_nothing() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("launch")).unwrap();
    let _held =
        pg_session_resume::lock::Lock::acquire(&dir.path().join("launch/pg-session-resume.lock"))
            .unwrap();
    let out = bin()
        .args(["tick", "--state"])
        .arg(dir.path())
        .arg("--pr-dir")
        .arg(dir.path())
        .output()
        .unwrap();
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("another tick holds the lock"));
}
