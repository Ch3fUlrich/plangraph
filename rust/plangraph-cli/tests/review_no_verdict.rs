mod review_support;
use review_support::*;
use std::{fs, process::Command};

use std::path::{Path, PathBuf};

const CAUSE: &str = "harness_not_found opencode /usr/bin:/bin";

struct Fixture {
    tmp: tempfile::TempDir,
    work: PathBuf,
    base: String,
    head: String,
    state: PathBuf,
}

fn fixture() -> Fixture {
    let (tmp, work, _bare, base) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    Fixture {
        tmp,
        work,
        base,
        head,
        state,
    }
}

fn review(f: &Fixture, mode: &str, wait: &str) -> std::process::Output {
    let log = f.tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(f.tmp.path(), &f.state, &log, mode);
    Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            f.work.to_str().unwrap(),
            "--base",
            &f.base,
            "--head",
            &f.head,
        ])
        .args([
            "--state",
            f.state.to_str().unwrap(),
            "--no-fetch",
            "--wait",
            wait,
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap()
}

fn review_line(out: &std::process::Output) -> String {
    let stdout = String::from_utf8_lossy(&out.stdout);
    stdout
        .lines()
        .find(|line| line.starts_with("REVIEW "))
        .unwrap_or_else(|| {
            panic!(
                "no REVIEW line: stdout={stdout} stderr={}",
                String::from_utf8_lossy(&out.stderr)
            )
        })
        .to_owned()
}

fn alerts(state: &Path) -> Option<String> {
    fs::read_to_string(state.join("alerts.jsonl")).ok()
}

#[test]
fn review_line_carries_the_refused_runs_reason() {
    let f = fixture();
    let line = review_line(&review(&f, "harness_refused", "5"));
    assert!(
        line.contains(CAUSE),
        "the refused run's reason must be on the REVIEW line: {line}"
    );
}

#[test]
fn a_pre_launch_refusal_without_a_run_record_still_ends_in_a_review_line_with_its_reason() {
    let f = fixture();
    let out = review(&f, "pre_launch_refusal", "5");
    let line = review_line(&out);
    assert!(
        line.contains("verdict=REQUEST_CHANGES") && line.contains(CAUSE),
        "{line}"
    );
    assert_eq!(out.status.code(), Some(1));
    review(&f, "pre_launch_refusal", "5");
    let alerts = alerts(&f.state).expect("two refused launches are a repeat");
    assert_eq!(alerts.lines().count(), 1, "{alerts}");
}

#[test]
fn two_refused_runs_raise_one_alert() {
    let f = fixture();
    review(&f, "harness_refused", "5");
    assert!(alerts(&f.state).is_none(), "one seat is not a repeat");
    let second = review(&f, "harness_refused", "5");
    let alerts = alerts(&f.state).expect("second seat alerts");
    assert_eq!(alerts.lines().count(), 1, "{alerts}");
    assert!(
        alerts.contains("no_verdict_repeat") && alerts.contains("harness_not_found"),
        "{alerts}"
    );
    assert!(String::from_utf8_lossy(&second.stderr).contains("no_verdict_alert"));
}

#[test]
fn a_genuinely_missing_verdict_is_booked_and_repeats_alert() {
    let f = fixture();
    review(&f, "no_verdict_line", "5");
    review(&f, "no_verdict_line", "5");
    let alerts = alerts(&f.state).expect("two answers without a verdict line alert");
    assert!(alerts.contains("no_verdict_line"), "{alerts}");
}

#[test]
fn an_explicit_verdict_is_never_booked_as_a_missing_one() {
    let f = fixture();
    // exit 0, and exit 1 (a denied-tools OpenCode review ends exit 1 with a final REQUEST_CHANGES)
    for mode in [
        "explicit_request_changes",
        "denied_review_verdict",
        "denied_review_verdict",
    ] {
        let line = review_line(&review(&f, mode, "5"));
        assert!(line.contains("verdict=REQUEST_CHANGES"), "{line}");
    }
    assert!(alerts(&f.state).is_none());
    assert!(!f.state.join("no_verdict.jsonl").exists());
}

#[test]
fn pending_seats_that_later_fail_are_booked_by_the_deferred_finisher() {
    let f = fixture();
    for _ in 0..2 {
        let out = review(&f, "late_failed", "0");
        let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
        assert_eq!(out.status.code(), Some(3), "{stdout}");
        let run_id = stdout
            .lines()
            .find_map(|l| {
                Some(
                    l.strip_prefix("PENDING run=")?
                        .split_whitespace()
                        .next()?
                        .to_owned(),
                )
            })
            .expect("pending run id");
        let run = f.state.join("runs").join(&run_id);
        fs::write(run.join("release"), "").unwrap();
        let late = run.join("late-review.txt");
        let began = std::time::Instant::now();
        while !late.exists() && began.elapsed().as_secs() < 30 {
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        assert!(late.exists(), "the finisher must publish its file");
    }
    let alerts = alerts(&f.state).expect("two deferred failures alert");
    assert_eq!(alerts.lines().count(), 1, "{alerts}");
    assert!(alerts.contains("harness_not_found"), "{alerts}");
}
