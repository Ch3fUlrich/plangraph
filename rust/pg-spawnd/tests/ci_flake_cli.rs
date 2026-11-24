//! The ci-flake binary end to end: what the workflow steps call.
use std::path::Path;
use std::process::{Command, Output};

fn flake(args: &[&str], envs: &[(&str, &Path)]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ci-flake"));
    command.args(args);
    for (key, value) in envs {
        command.env(key, value);
    }
    command.output().unwrap()
}

#[test]
fn classify_writes_the_record_and_the_priority_ten_item_for_fail_then_pass() {
    let dir = tempfile::tempdir().unwrap();
    let (first, second, out) = (
        dir.path().join("first.log"),
        dir.path().join("second.log"),
        dir.path().join("out"),
    );
    std::fs::write(
        &first,
        "FAILED tests/t.py::a - boom\nFAILED tests/t.py::b - boom\n",
    )
    .unwrap();
    std::fs::write(
        &second,
        "PASSED tests/t.py::a\nFAILED tests/t.py::b - boom\n",
    )
    .unwrap();
    let github = dir.path().join("github-output");
    let result = flake(
        &[
            "classify",
            "--suite",
            "pytest",
            "--runner",
            "r-1",
            "--run-id",
            "9",
            "--attempt",
            "2",
            "--sha",
            &"c".repeat(40),
            "--first",
            first.to_str().unwrap(),
            "--second",
            second.to_str().unwrap(),
            "--out",
            out.to_str().unwrap(),
        ],
        &[("GITHUB_OUTPUT", &github)],
    );
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let record: serde_json::Value =
        serde_json::from_slice(&std::fs::read(out.join("flake-record.json")).unwrap()).unwrap();
    assert_eq!(record["verdict"], "fail-fail");
    assert_eq!(record["result"], "red");
    let items: serde_json::Value =
        serde_json::from_slice(&std::fs::read(out.join("flake-items.json")).unwrap()).unwrap();
    assert_eq!(items["items"][0]["priority"], 10);
    assert_eq!(items["items"].as_array().unwrap().len(), 1);
    assert!(std::fs::read_to_string(&github)
        .unwrap()
        .contains("verdict=fail-fail"));
    assert!(String::from_utf8_lossy(&result.stdout).contains("FLAKY (failed, then passed)"));
}

#[test]
fn quarantine_prints_owner_and_expiry_and_counts_the_active_entries() {
    let dir = tempfile::tempdir().unwrap();
    let ledger = dir.path().join("ledger.json");
    std::fs::write(&ledger, r#"{"tests/t.py::a": {"owner": "plangraph:FIX", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-25T00:00:00Z"},
        "cargo:pg-spawnd:m::c": {"owner": "plangraph:FIX2", "starts": "2026-11-20T00:00:00Z", "expires": "2026-11-21T00:00:00Z"}}"#).unwrap();
    let github = dir.path().join("github-output");
    let result = flake(
        &[
            "quarantine",
            "--ledger",
            ledger.to_str().unwrap(),
            "--as-of",
            "2026-11-24T06:00:00+00:00",
        ],
        &[("GITHUB_OUTPUT", &github)],
    );
    let text = String::from_utf8_lossy(&result.stdout);
    assert!(
        result.status.success()
            && text.contains("plangraph:FIX | 2026-11-25T00:00:00+00:00 | active"),
        "{text}"
    );
    assert!(text.contains("expired: blocks as normal"));
    assert_eq!(std::fs::read_to_string(&github).unwrap().trim(), "active=1");
    let cargo = flake(
        &[
            "cargo-quarantine",
            "--ledger",
            ledger.to_str().unwrap(),
            "--as-of",
            "2026-11-20T12:00:00Z",
            "--mode",
            "quarantine",
        ],
        &[],
    );
    assert_eq!(String::from_utf8_lossy(&cargo.stdout), "pg-spawnd m::c\n");
}

#[test]
fn an_invalid_ledger_or_timestamp_fails_instead_of_quarantining() {
    let dir = tempfile::tempdir().unwrap();
    let ledger = dir.path().join("ledger.json");
    std::fs::write(&ledger, r#"{"t": {"owner": "x", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-24T01:00:00Z"}}"#).unwrap();
    assert_eq!(
        flake(
            &[
                "quarantine",
                "--ledger",
                ledger.to_str().unwrap(),
                "--as-of",
                "2026-11-24T00:00:00Z"
            ],
            &[]
        )
        .status
        .code(),
        Some(2)
    );
    assert_eq!(
        flake(
            &[
                "quarantine",
                "--ledger",
                ledger.to_str().unwrap(),
                "--as-of",
                "yesterday"
            ],
            &[]
        )
        .status
        .code(),
        Some(2)
    );
}

#[test]
fn combine_durations_writes_one_map_for_the_next_train() {
    let dir = tempfile::tempdir().unwrap();
    let (a, b, out) = (
        dir.path().join("a.json"),
        dir.path().join("b.json"),
        dir.path().join("out.json"),
    );
    std::fs::write(&a, r#"{"t.py::x": 1.0}"#).unwrap();
    std::fs::write(&b, r#"{"t.py::y": 2.0}"#).unwrap();
    let result = flake(
        &[
            "combine-durations",
            "--out",
            out.to_str().unwrap(),
            a.to_str().unwrap(),
            b.to_str().unwrap(),
        ],
        &[],
    );
    assert!(result.status.success());
    assert_eq!(
        std::fs::read_to_string(&out).unwrap(),
        "{\"t.py::x\":1.0,\"t.py::y\":2.0}\n"
    );
}

#[test]
fn an_expired_or_not_yet_started_cargo_entry_is_not_quarantined_on_any_runner() {
    let dir = tempfile::tempdir().unwrap();
    let ledger = dir.path().join("ledger.json");
    std::fs::write(&ledger, r#"{"cargo:pg-spawnd:m::c": {"owner": "plangraph:FIX", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-25T00:00:00Z"}}"#).unwrap();
    let ask = |as_of: &str, mode: &str| {
        let out = flake(
            &[
                "cargo-quarantine",
                "--ledger",
                ledger.to_str().unwrap(),
                "--as-of",
                as_of,
                "--mode",
                mode,
            ],
            &[],
        );
        assert!(out.status.success());
        String::from_utf8_lossy(&out.stdout).into_owned()
    };
    assert_eq!(ask("2026-11-24T12:00:00Z", "blocking"), "pg-spawnd m::c\n");
    // At the expiry instant and after it the test blocks again (it is no longer excluded); before the start it was never excluded.
    assert_eq!(ask("2026-11-25T00:00:00Z", "blocking"), "");
    assert_eq!(ask("2026-11-23T23:59:59Z", "blocking"), "");
}
