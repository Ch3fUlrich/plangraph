use super::*;

#[test]
fn live_leg_returns_pending_and_publishes_late_review_file() {
    check_live_leg("late", 3, "PENDING");
}

#[test]
fn mixed_failed_and_live_seats_report_failure_and_keep_the_live_run() {
    check_live_leg("late_mixed", 1, "REQUEST_CHANGES");
}

fn check_live_leg(mode: &str, expected_exit: i32, expected_verdict: &str) {
    let (tmp, work, _, base) = init_repo_with_origin();
    fs::write(work.join("file.rs"), "fn changed() {}\n").unwrap();
    git(&work, &["add", "file.rs"]);
    git(
        &work,
        &["commit", "-m", "change\n\nWriter: gemini run-feature"],
    );
    let head = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let spawner = write_mock_spawner(tmp.path(), &state, &log, mode);
    let approval = tmp.path().join("approval.txt");
    let output = Command::new(plangraph_bin())
        .current_dir(tmp.path())
        .args(["review", "--repo"])
        .arg(&work)
        .args([
            "--base",
            &base,
            "--head",
            &head,
            "--no-fetch",
            "--wait",
            "0",
        ])
        .args(["--state", "state", "--seats"])
        .arg("--approval")
        .arg(&approval)
        .env("PLANGRAPH_SPAWNER_CMD", spawner)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(
        output.status.code(),
        Some(expected_exit),
        "{stdout}\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let run_id = stdout
        .lines()
        .find_map(|line| {
            let (before, _) = line.split_once(" status=running")?;
            Some(before.split_once("run=")?.1)
        })
        .expect("running seat in review report");
    assert!(
        stdout.lines().any(|line| line.starts_with("REVIEW ")
            && line.contains(&format!("verdict={expected_verdict} "))),
        "{stdout}"
    );
    if mode == "late_mixed" {
        assert!(stdout.contains("run=run-failed status=failed"), "{stdout}");
    }
    let run = state.join("runs").join(run_id);
    let record: Value =
        serde_json::from_slice(&fs::read(run.join("record.json")).unwrap()).unwrap();
    let reported: f64 = stdout
        .split("expected_end=")
        .nth(1)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .parse()
        .unwrap();
    assert!(
        (reported - record["expected_end"].as_f64().unwrap()).abs() < 0.001,
        "{stdout}"
    );

    fs::write(run.join("release"), "").unwrap();
    let artifact = run.join("late-review.txt");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(6);
    while !artifact.is_file() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    let text = fs::read_to_string(artifact).unwrap();
    assert!(text.contains(&format!("RUN: {run_id}\n")), "{text}");
    assert!(
        text.contains(&format!("HEAD: {head}\n")) && text.contains("VERDICT: PASS\n"),
        "{text}"
    );
    assert!(!approval.exists());
}
