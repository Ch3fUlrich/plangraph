use serde_json::json;
use std::{
    io::Write,
    process::{Command, Stdio},
};

#[test]
fn train_report_pins_green_main_and_preserves_elapsed_time_across_overlaps() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["landing", "report", "example/repo", "train-1", "--fixture"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let head = "a".repeat(40);
    let merge = "b".repeat(40);
    let row = |stage: &str, head: &str, attempt: u64, start: u64, end: u64| json!({"stage":stage,"head":head,"attempt":attempt,"started_at_ms":start,"ended_at_ms":end});
    let fixture = json!({
        "stages": [
            row("review", &head, 1, 10_000, 20_000),
            row("ci", &head, 7_001, 20_000, 200_000),
            row("delta_review", &head, 1, 30_000, 150_000),
            row("merge", &head, 1, 200_000, 210_000),
            row("ci", &merge, 8_001, 210_000, 270_000),
        ],
        "train": {"merge":merge},
        "accepts": [20_000],
        "jobs":[
            {"run_id":7,"attempt":1,"head":head,"name":"ci-gate","conclusion":"success","run_conclusion":"success"},
            {"run_id":8,"attempt":1,"head":merge,"name":"ci-gate","conclusion":"success","run_conclusion":"success"}
        ]
    });
    child
        .stdin
        .take()
        .unwrap()
        .write_all(fixture.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["slowest"]["stage"], "ci");
    assert_eq!(result["slowest"]["minutes"], 3.0);
    assert_eq!(result["milestones"]["green_main_at_ms"], 270_000);
    assert_eq!(result["accept_to_green_ms"], 250_000);
}
