mod review_support;
use review_support::*;
use serde_json::Value;
use std::{fs, path::PathBuf, process::Command};
use tempfile::TempDir;

#[test]
fn stale_head_is_refused() {
    let (tmp, work, bare, base_sha) = init_repo_with_origin();

    // Create a feature branch commit from base_sha.
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("src_a.rs"), "pub fn a() -> i32 { 1 }\n").unwrap();
    git(&work, &["add", "src_a.rs"]);
    git(&work, &["commit", "-m", "feat\n\nWriter: gemini run-1"]);
    let feat_head = git(&work, &["rev-parse", "HEAD"]);

    // Advance origin/main in a second clone so feat_head is behind origin/main.
    let other = tmp.path().join("other");
    let st = Command::new("git")
        .args(["clone"])
        .arg(&bare)
        .arg(&other)
        .status()
        .unwrap();
    assert!(st.success());
    git(&other, &["config", "user.name", "Other"]);
    git(&other, &["config", "user.email", "other@example.com"]);
    fs::write(other.join("fixture-seed.txt"), "ahead\n").unwrap();
    git(&other, &["add", "fixture-seed.txt"]);
    git(&other, &["commit", "-m", "advance\n\nWriter: gpt run-2"]);
    git(&other, &["push", "origin", "main"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "pass");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &feat_head,
            "--state",
            state.to_str().unwrap(),
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(out.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("base_stale"), "stderr={stderr}");
    assert!(!log.exists(), "spawner should not be called on stale head");
}

#[test]
fn writer_families_extracted_from_trailers_and_fallback() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();

    git(&work, &["checkout", "-b", "feat"]);

    // Commit 1: Author says Claude, Writer trailer says gemini -> must resolve ONLY google!
    git(&work, &["config", "user.name", "Claude Opus 5.5"]);
    fs::write(work.join("one.rs"), "pub fn one() {}\n").unwrap();
    git(&work, &["add", "one.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c1\n\nWriter: gemini-4-hydrogen-high run-abc",
        ],
    );

    // Commit 2: Hand-Edit mentions codex (openai) + non-family changelog note ignored.
    git(&work, &["config", "user.name", "Human User"]);
    fs::write(work.join("two.rs"), "pub fn two() {}\n").unwrap();
    git(&work, &["add", "two.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c2\n\nHand-Edit: changelog\nHand-Edit: codex manual tweak",
        ],
    );

    // Commit 3: No Writer trailer, falls back to Co-authored-by: deepseek.
    fs::write(work.join("three.rs"), "pub fn three() {}\n").unwrap();
    git(&work, &["add", "three.rs"]);
    git(
        &work,
        &[
            "commit",
            "-m",
            "c3\n\nCo-authored-by: DeepSeek <ds@example.com>",
        ],
    );

    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "fenced_pass");
    let approval = tmp.path().join("approval.txt");

    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
            "--item",
            "plangraph:REVIEW-CMD",
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(
        out.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );

    let entries: Vec<Value> = fs::read_to_string(&log)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .collect();
    // Three writer families: one route + spawn pass per family, each excluding only its own.
    assert_eq!(entries.len(), 6);
    let mut seen = std::collections::BTreeSet::new();
    let mut seat_families = std::collections::BTreeSet::new();
    for pair in entries.chunks(2) {
        assert_eq!(pair[0]["verb"], "route");
        assert_eq!(pair[1]["verb"], "spawn");
        let fams = |entry: &Value| {
            entry["writer_families"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
        };
        assert_eq!(fams(&pair[0]), fams(&pair[1]));
        assert_eq!(fams(&pair[0]).len(), 1, "{pair:?}");
        // The seat comes from outside its own group's family (the mock prefers google, openai,
        // then deepseek: it would pick xiaomi only if every writer family were excluded at once).
        let seat = pair[1]["seat_family"].as_str().unwrap().to_owned();
        assert!(!fams(&pair[0]).contains(&seat), "{pair:?}");
        seat_families.insert(seat);
        seen.extend(fams(&pair[0]));
    }
    assert_eq!(
        seat_families.into_iter().collect::<Vec<_>>(),
        vec!["google", "openai"]
    );
    assert_eq!(
        seen.into_iter().collect::<Vec<_>>(),
        vec!["deepseek", "google", "openai"]
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("seats=3 verdict=PASS"));

    // Verify merge_batch_land.py check_approval accepts the generated approval file.
    let tools_dir = repo_root().join("tools");
    let py = Command::new("python3")
        .args([
            "-c",
            &format!(
                "import sys; sys.path.insert(0, {:?}); from merge_batch_land import check_approval; sys.exit(0 if check_approval({:?}, {:?}) is None else 1)",
                tools_dir.display().to_string(),
                approval.display().to_string(),
                head_sha
            ),
        ])
        .status()
        .unwrap();
    assert!(
        py.success(),
        "merge_batch_land.check_approval rejected approval:\n{}",
        fs::read_to_string(&approval).unwrap()
    );
}

#[test]
fn oversize_diff_splits_across_seats_and_single_oversize_file_refuses() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::create_dir_all(work.join("alpha")).unwrap();
    fs::create_dir_all(work.join("beta")).unwrap();
    fs::create_dir_all(work.join("gamma")).unwrap();

    // 400 tokens -> leg max chars = 1200; combined diff across 3 files > 1200 chars, each file < 550 chars.
    fs::write(work.join("alpha/a.rs"), format!("// {}\n", "a".repeat(260))).unwrap();
    fs::write(work.join("beta/b.rs"), format!("// {}\n", "b".repeat(260))).unwrap();
    fs::write(work.join("gamma/c.rs"), format!("// {}\n", "c".repeat(260))).unwrap();
    git(&work, &["add", "."]);
    git(&work, &["commit", "-m", "split\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "split_pass");
    let approval = tmp.path().join("approval-split.txt");

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
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );

    let spawn_paths: Vec<Vec<String>> = fs::read_to_string(&log)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .filter(|v| v["verb"] == "spawn")
        .map(|v| {
            v["diff_ref"]["paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().unwrap().to_owned())
                .collect()
        })
        .collect();
    assert!(
        spawn_paths.len() >= 2,
        "expected split seats, got {spawn_paths:?}"
    );
    let mut covered: Vec<String> = spawn_paths.into_iter().flatten().collect();
    covered.sort();
    assert_eq!(covered, vec!["alpha/a.rs", "beta/b.rs", "gamma/c.rs"]);

    // The beta seat returns REQUEST_CHANGES regardless of spawn order.
    let approval_fail = tmp.path().join("approval-fail.txt");
    let mock_fail = write_mock_spawner(tmp.path(), &state, &log, "split_fail_beta");
    fs::write(&log, "").unwrap();
    let out_fail = Command::new(plangraph_bin())
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
            "--approval",
            approval_fail.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_fail)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_fail.status.code(), Some(1));
    assert!(!approval_fail.exists());
    let failed_seats: Vec<_> = std::str::from_utf8(&out_fail.stdout)
        .unwrap()
        .lines()
        .filter(|line| line.starts_with("SEAT ") && line.contains("verdict=REQUEST_CHANGES"))
        .collect();
    assert_eq!(failed_seats.len(), 1, "{failed_seats:?}");
    assert!(
        failed_seats[0].ends_with("paths=beta/b.rs"),
        "{failed_seats:?}"
    );

    // Now test a single file larger than leg limit (1200 chars) -> refused naming the file.
    fs::write(
        work.join("alpha/huge.rs"),
        format!("// {}\n", "z".repeat(1400)),
    )
    .unwrap();
    git(&work, &["add", "alpha/huge.rs"]);
    git(&work, &["commit", "-m", "huge\n\nWriter: gemini run-2"]);
    let huge_head = git(&work, &["rev-parse", "HEAD"]);
    fs::write(&log, "").unwrap();
    let mock_huge = write_mock_spawner(tmp.path(), &state, &log, "single_too_big");
    let out_huge = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &huge_head,
            "--state",
            state.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_huge)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_huge.status.code(), Some(2));
    let stderr_huge = String::from_utf8_lossy(&out_huge.stderr);
    assert!(
        stderr_huge.contains("alpha/huge.rs"),
        "stderr={stderr_huge}"
    );
    assert!(
        !fs::read_to_string(&log).unwrap().contains("\"spawn\""),
        "must refuse before spawning seats"
    );
}

#[test]
fn test_gaming_yes_blocks_pass_even_with_verdict_pass() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let mock = write_mock_spawner(tmp.path(), &state, &log, "test_gaming_yes");
    let approval = tmp.path().join("approval-gaming.txt");

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
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();

    assert_eq!(out.status.code(), Some(1));
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("test_gaming=yes"), "stdout={stdout}");
    assert!(
        stdout.contains("verdict=REQUEST_CHANGES"),
        "stdout={stdout}"
    );
    assert!(!approval.exists());
}

#[test]
fn redacted_head_line_blocks_pass_and_seats_envelope_extracts_records() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::write(work.join("f.rs"), "pub fn f() {}\n").unwrap();
    git(&work, &["add", "f.rs"]);
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);

    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let approval = tmp.path().join("approval-redacted.txt");
    let mock_redacted = write_mock_spawner(tmp.path(), &state, &log, "redacted_head");
    let out_redacted = Command::new(plangraph_bin())
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
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_redacted)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(out_redacted.status.code(), Some(1));
    assert!(!approval.exists());

    let approval_seats = tmp.path().join("approval-seats.txt");
    let mock_seats = write_mock_spawner(tmp.path(), &state, &log, "seats_envelope");
    let out_seats = Command::new(plangraph_bin())
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
            "--approval",
            approval_seats.to_str().unwrap(),
            "--seats",
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock_seats)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    assert_eq!(
        out_seats.status.code(),
        Some(0),
        "stderr={}\nstdout={}",
        String::from_utf8_lossy(&out_seats.stderr),
        String::from_utf8_lossy(&out_seats.stdout)
    );
    let body = fs::read_to_string(&approval_seats).unwrap();
    assert!(body.contains(&format!("HEAD: {head_sha}\nVERDICT: PASS")));
}

fn review_with_mode(
    mode: &str,
    files: &[(&str, &[u8])],
) -> (std::process::Output, PathBuf, PathBuf, TempDir) {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    for (name, body) in files {
        fs::write(work.join(name), body).unwrap();
        git(&work, &["add", name]);
    }
    git(&work, &["commit", "-m", "f\n\nWriter: gemini run-1"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let state = tmp.path().join("state");
    let log = tmp.path().join("spawner.jsonl");
    let approval = tmp.path().join("approval.txt");
    let mock = write_mock_spawner(tmp.path(), &state, &log, mode);
    let out = Command::new(plangraph_bin())
        .args([
            "review",
            "--repo",
            work.to_str().unwrap(),
            "--base",
            &base_sha,
            "--head",
            &head_sha,
        ])
        .args([
            "--state",
            state.to_str().unwrap(),
            "--approval",
            approval.to_str().unwrap(),
            "--no-fetch",
        ])
        .env("PLANGRAPH_SPAWNER_CMD", &mock)
        .env("PYTHONPATH", repo_root().join("src"))
        .output()
        .unwrap();
    (out, approval, log, tmp)
}

#[test]
fn reply_without_head_line_is_not_a_pass() {
    let (out, approval, _log, _tmp) = review_with_mode("no_head", &[("f.rs", b"pub fn f() {}\n")]);
    assert_eq!(
        out.status.code(),
        Some(1),
        "stdout={}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(!approval.exists());
}

#[test]
fn route_without_legs_is_refused_before_any_seat() {
    let (out, approval, log, _tmp) = review_with_mode("null_leg", &[("f.rs", b"pub fn f() {}\n")]);
    assert_eq!(out.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("no eligible review legs"),
        "stderr={stderr}"
    );
    assert!(!approval.exists());
    assert!(!fs::read_to_string(&log).unwrap().contains("\"spawn\""));
}

#[test]
fn rename_old_path_is_in_a_group_like_the_spawner_lists_it() {
    let (tmp, work, _bare, _base) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    fs::create_dir_all(work.join("a")).unwrap();
    fs::create_dir_all(work.join("b")).unwrap();
    fs::create_dir_all(work.join("c")).unwrap();
    fs::write(work.join("a/old.rs"), format!("// {}\n", "r".repeat(260))).unwrap();
    git(&work, &["add", "."]);
    git(&work, &["commit", "-m", "old\n\nWriter: gemini run-1"]);
    let base = git(&work, &["rev-parse", "HEAD"]);
    git(&work, &["mv", "a/old.rs", "b/new.rs"]);
    fs::write(work.join("c/x.rs"), format!("// {}\n", "x".repeat(260))).unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-m", "mv\n\nWriter: gemini run-2"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    let (out, log) = review_in_repo(&work, tmp.path(), &base, &head);
    assert!(
        out.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let logged = fs::read_to_string(&log).unwrap();
    assert!(
        logged.contains("a/old.rs") && logged.contains("b/new.rs"),
        "{logged}"
    );
}

#[test]
fn binary_file_is_measured_as_the_inlined_base85_patch() {
    let (tmp, work, _bare, base) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    let bytes: Vec<u8> = (0..2000u32)
        .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
        .collect();
    fs::write(work.join("blob.bin"), bytes).unwrap();
    fs::write(work.join("small.rs"), "pub fn s() {}\n").unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-m", "bin\n\nWriter: gemini run-1"]);
    let head = git(&work, &["rev-parse", "HEAD"]);
    let (out, log) = review_in_repo(&work, tmp.path(), &base, &head);
    // A "Binary files differ" stub would fit the 1200-char limit; the base85 patch does not.
    assert_eq!(
        out.status.code(),
        Some(2),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stderr).contains("blob.bin"));
    assert!(!fs::read_to_string(&log).unwrap().contains("\"spawn\""));
}

#[test]
fn single_writer_family_range_yields_one_seat_and_a_failing_group_blocks() {
    let (tmp, work, _bare, base_sha) = init_repo_with_origin();
    git(&work, &["checkout", "-b", "feat"]);
    for (name, trailer) in [
        ("a.rs", "Writer: gemini run-1"),
        ("b.rs", "Writer: gemini run-2"),
    ] {
        fs::write(work.join(name), "pub fn f() {}\n").unwrap();
        git(&work, &["add", name]);
        git(&work, &["commit", "-m", &format!("c\n\n{trailer}")]);
    }
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let run = |mode: &str, dir: &str| {
        let root = tmp.path().join(dir);
        fs::create_dir_all(&root).unwrap();
        let state = root.join("state");
        let log = root.join("spawner.jsonl");
        let mock = write_mock_spawner(&root, &state, &log, mode);
        let out = Command::new(plangraph_bin())
            .args([
                "review",
                "--repo",
                work.to_str().unwrap(),
                "--base",
                &base_sha,
                "--head",
                &head_sha,
            ])
            .args(["--state", state.to_str().unwrap(), "--no-fetch"])
            .env("PLANGRAPH_SPAWNER_CMD", &mock)
            .env("PYTHONPATH", repo_root().join("src"))
            .output()
            .unwrap();
        let entries = fs::read_to_string(&log).unwrap().lines().count();
        (out, entries)
    };
    let (out, entries) = run("pass", "single");
    assert_eq!(entries, 2, "one route + one spawn for one family");
    assert!(String::from_utf8_lossy(&out.stdout).contains("seats=1 verdict=PASS"));

    // Add a second family: the combined verdict is ACCEPT only when every seat accepts.
    fs::write(work.join("c.rs"), "pub fn g() {}\n").unwrap();
    git(&work, &["add", "c.rs"]);
    git(&work, &["commit", "-m", "c\n\nWriter: gpt run-3"]);
    let head_sha = git(&work, &["rev-parse", "HEAD"]);
    let run_two = |mode: &str, dir: &str| {
        let root = tmp.path().join(dir);
        fs::create_dir_all(&root).unwrap();
        let state = root.join("state");
        let log = root.join("spawner.jsonl");
        let mock = write_mock_spawner(&root, &state, &log, mode);
        Command::new(plangraph_bin())
            .args([
                "review",
                "--repo",
                work.to_str().unwrap(),
                "--base",
                &base_sha,
                "--head",
                &head_sha,
            ])
            .args(["--state", state.to_str().unwrap(), "--no-fetch"])
            .env("PLANGRAPH_SPAWNER_CMD", &mock)
            .env("PYTHONPATH", repo_root().join("src"))
            .output()
            .unwrap()
    };
    let accepted = run_two("pass", "two-pass");
    assert!(String::from_utf8_lossy(&accepted.stdout).contains("seats=2 verdict=PASS"));
    let blocked = run_two("fail_openai_group", "two-fail");
    let text = String::from_utf8_lossy(&blocked.stdout);
    assert!(text.contains("verdict=REQUEST_CHANGES"), "{text}");
    assert_eq!(blocked.status.code(), Some(1));
}

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
    let run_id = stdout.lines().find_map(|line| {
        let (before, _) = line.split_once(" status=running")?;
        Some(before.split_once("run=")?.1)
    }).expect("running seat in review report");
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

#[test]
fn late_review_file_marks_a_missing_verdict() {
    check_late_verdict("completed", 0, "Reviewed the change", "MISSING");
}

#[test]
fn late_review_file_marks_a_failed_or_killed_run() {
    check_late_verdict("failed", 7, "VERDICT: REQUEST_CHANGES", "ERROR");
    check_late_verdict("killed", 137, "", "ERROR");
}

#[test]
fn late_review_file_preserves_a_request_for_changes() {
    check_late_verdict(
        "completed",
        0,
        "VERDICT: REQUEST_CHANGES",
        "REQUEST_CHANGES",
    );
}

fn check_late_verdict(status: &str, code: i32, answer: &str, expected: &str) {
    let state = tempfile::tempdir().unwrap();
    let head = "a".repeat(40);
    let run = state.path().join("runs/run-terminal");
    fs::create_dir_all(&run).unwrap();
    fs::write(
        run.join("record.json"),
        serde_json::json!({
            "run_id":"run-terminal", "status":status, "exit_code":code, "stdout_tail":answer,
        })
        .to_string(),
    )
    .unwrap();
    let output = Command::new(plangraph_bin())
        .args(["review", "--finish-seat"])
        .arg(state.path())
        .args([&head, "run-terminal", "0"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let artifact = fs::read_to_string(run.join("late-review.txt")).unwrap();
    assert_eq!(
        artifact,
        format!("RUN: run-terminal\nHEAD: {head}\nSTATUS: {status}\nVERDICT: {expected}\n")
    );
}
