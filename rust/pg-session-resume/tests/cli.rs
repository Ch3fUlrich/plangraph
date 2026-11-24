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

#[test]
fn done_status_with_a_missing_claude_binary_logs_a_failed_stop() {
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
    let out = bin()
        .args(["stop-done", "--quiet-s", "0", "--state"])
        .arg(dir.path())
        .arg("--pr-dir")
        .arg(&pr)
        .env("PATH", "/nonexistent")
        .output()
        .unwrap();
    assert!(out.status.success());
    let log = std::fs::read_to_string(dir.path().join("session-lifecycle.log")).unwrap();
    assert!(
        log.contains("event=stop-failed name=w-1 reason=done"),
        "{log}"
    );
}
