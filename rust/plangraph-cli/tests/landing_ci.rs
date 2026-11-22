use std::{
    io::Write,
    process::{Command, Stdio},
};

#[test]
fn import_jobs_pins_the_run_attempt_and_reports_duration() {
    let head = "a".repeat(40);
    let mut child = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args([
            "landing",
            "ci-import",
            "example/repo",
            "train-1",
            &head,
            "25",
            "2",
            "--fixture",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let fixture = serde_json::json!({
        "run": {"id": 25, "run_attempt": 2, "head_sha": head, "status": "completed", "conclusion": "success",
            "run_started_at": "2026-01-01T01:00:00Z", "updated_at": "2026-01-01T01:04:00Z"},
        "jobs": [{"id": 601, "run_id": 25, "run_attempt": 2, "head_sha": head, "name": "tests",
            "status": "completed", "conclusion": "success",
            "started_at": "2026-01-01T01:00:30Z", "completed_at": "2026-01-01T01:03:45Z"}]
    });
    child
        .stdin
        .take()
        .unwrap()
        .write_all(fixture.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let receipt: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(receipt["stage"]["attempt"], 25_002);
    assert_eq!(receipt["jobs"][0]["job_id"], 601);
    assert_eq!(receipt["jobs"][0]["duration_ms"], 195_000);
}
