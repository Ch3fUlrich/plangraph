mod review_support;
use review_support::*;
use std::{fs, process::Command};

#[test]
fn review_line_carries_the_refused_runs_reason() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "harness_refused");
    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--state",
            state.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    let review = stdout
        .lines()
        .find(|line| line.starts_with("REVIEW "))
        .unwrap_or_else(|| panic!("no REVIEW line: stdout={stdout} stderr={stderr}"));
    assert!(
        review.contains("harness_not_found opencode /usr/bin:/bin"),
        "the refused run's reason must be on the REVIEW line: {review}"
    );
}

#[test]
fn two_review_runs_without_a_verdict_for_one_cause_raise_one_alert() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "harness_refused");
    let review = || {
        Command::new(plangraph_bin())
            .args([
                "review",
                "--repo",
                work.to_str().unwrap(),
                "--base",
                &base_sha,
                "--head",
                &head_sha,
                "--state",
                state.to_str().unwrap(),
                "--no-fetch",
            ])
            .env("PLANGRAPH_SPAWNER_CMD", &mock)
            .env("PYTHONPATH", repo_root().join("src"))
            .output()
            .unwrap()
    };
    review();
    assert!(
        !state.join("alerts.jsonl").exists(),
        "one seat is not a repeat"
    );
    let second = review();
    let alerts = fs::read_to_string(state.join("alerts.jsonl")).expect("second seat alerts");
    assert_eq!(alerts.lines().count(), 1, "{alerts}");
    assert!(
        alerts.contains("no_verdict_repeat") && alerts.contains("harness_not_found"),
        "{alerts}"
    );
    assert!(String::from_utf8_lossy(&second.stderr).contains("no_verdict_alert"));
}
