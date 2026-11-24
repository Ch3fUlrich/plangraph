//! `plangraph wait` against a fake `gh` on PATH and real git remotes.
use std::{fs, path::Path, process::Command};

/// A `gh` that answers `run list` and `pr view` and fails on anything else, logging every call.
fn fake_gh(dir: &Path, runs: &str) -> std::path::PathBuf {
    let bin = dir.join("bin");
    fs::create_dir_all(&bin).unwrap();
    fs::write(dir.join("runs.json"), runs).unwrap();
    fs::write(dir.join("tip"), "abc\n").unwrap();
    fs::write(dir.join("comments.json"), r#"{"comments":[]}"#).unwrap();
    let script = format!(
        "#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1 $2\" in\n  \"run list\") [ -f {delay} ] && sleep $(cat {delay}); cat {runs} ;;\n  api\\ *) cat {tip} ;;\n  \"pr view\") case \"$*\" in *comments*) cat {comments} ;; *) echo '{{\"headRefName\":\"lane/x\",\"headRefOid\":\"abc\"}}' ;; esac ;;\n  *) exit 9 ;;\nesac\n",
        log = dir.join("gh.log").display(),
        runs = dir.join("runs.json").display(),
        delay = dir.join("delay").display(),
        tip = dir.join("tip").display(),
        comments = dir.join("comments.json").display()
    );
    pg_worker_core::executable_test_support::write_executable(bin.join("gh"), script);
    bin
}

fn wait(dir: &Path, bin: &Path, args: &[&str]) -> std::process::Output {
    let path = format!("{}:{}", bin.display(), std::env::var("PATH").unwrap());
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .arg("wait")
        .args(args)
        .env("PATH", path)
        .env("CLAUDE_JOB_DIR", dir.join("job"))
        .output()
        .unwrap()
}

#[test]
fn a_green_run_with_skipped_jobs_ends_the_wait_and_never_reads_the_rollup() {
    let dir = tempfile::tempdir().unwrap();
    // The newest run is completed and green; an older one is still listed as queued.
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":45370719249,"status":"completed","conclusion":"success","headSha":"abc"},{"databaseId":45370000000,"status":"queued","conclusion":"","headSha":"abc"}]"#,
    );
    let out = wait(
        dir.path(),
        &bin,
        &["ci", "--pr", "1270", "--deadline", "30", "--poll", "1"],
    );
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(
        text.contains("WAIT ci green run=45370719249 conclusion=success"),
        "{text}"
    );
    let log = fs::read_to_string(dir.path().join("gh.log")).unwrap();
    assert!(!log.contains("statusCheckRollup"), "{log}");
    let record = fs::read_to_string(dir.path().join("job/waits/ci-pr1270.json")).unwrap();
    assert!(record.contains("\"outcome\": \"green\""), "{record}");
}

#[test]
fn a_run_that_never_completes_ends_at_the_deadline_with_a_named_timeout() {
    let dir = tempfile::tempdir().unwrap();
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":7,"status":"in_progress","conclusion":"","headSha":"abc"}]"#,
    );
    let started = std::time::Instant::now();
    let out = wait(
        dir.path(),
        &bin,
        &[
            "ci",
            "--branch",
            "lane/x",
            "--sha",
            "abc",
            "--deadline",
            "2",
            "--poll",
            "1",
        ],
    );
    assert_eq!(out.status.code(), Some(3), "{out:?}");
    assert!(started.elapsed().as_secs() < 30);
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(
        text.contains("WAIT ci timeout deadline 2s: still pending: run=7 status=in_progress"),
        "{text}"
    );
    let record = fs::read_to_string(dir.path().join("job/waits/ci-lane_x.json")).unwrap();
    assert!(record.contains("\"outcome\": \"timeout\""), "{record}");
}

#[test]
fn a_branch_wait_filters_by_the_tip_and_never_returns_the_previous_commits_green() {
    let dir = tempfile::tempdir().unwrap();
    // The branch tip moved to `new`; only the older commit has a (green) run.
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":5,"status":"completed","conclusion":"success","headSha":"old"}]"#,
    );
    fs::write(dir.path().join("tip"), "new\n").unwrap();
    let out = wait(
        dir.path(),
        &bin,
        &["ci", "--branch", "lane/x", "--deadline", "2", "--poll", "1"],
    );
    assert_eq!(out.status.code(), Some(3), "{out:?}");
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(text.contains("no run seen for this head"), "{text}");
    let log = fs::read_to_string(dir.path().join("gh.log")).unwrap();
    assert!(log.contains("commits/lane/x"), "{log}");
    // Once the tip has its own run, that run decides.
    fs::write(
        dir.path().join("runs.json"),
        r#"[{"databaseId":5,"status":"completed","conclusion":"success","headSha":"old"},{"databaseId":6,"status":"completed","conclusion":"failure","headSha":"new"}]"#,
    )
    .unwrap();
    let out = wait(
        dir.path(),
        &bin,
        &["ci", "--branch", "lane/x", "--deadline", "5", "--poll", "1"],
    );
    assert_eq!(out.status.code(), Some(1), "{out:?}");
}

#[test]
fn a_slow_gh_ends_at_the_deadline_not_at_its_own_timeout() {
    let dir = tempfile::tempdir().unwrap();
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":7,"status":"in_progress","conclusion":"","headSha":"abc"}]"#,
    );
    // `gh run list` hangs for 40 s; the wait has 3 s and must kill the call at the deadline.
    fs::write(dir.path().join("delay"), "40").unwrap();
    let started = std::time::Instant::now();
    let out = wait(
        dir.path(),
        &bin,
        &["ci", "--pr", "9", "--deadline", "3", "--poll", "60"],
    );
    assert_eq!(out.status.code(), Some(3), "{out:?}");
    assert!(started.elapsed().as_secs() < 10, "{:?}", started.elapsed());
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(text.contains("deadline 3s"), "{text}");
}

#[test]
fn a_green_run_with_no_go_request_raises_the_gate_alert_until_one_is_posted() {
    let dir = tempfile::tempdir().unwrap();
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":9,"status":"completed","conclusion":"success","headSha":"abc","updatedAt":"2020-01-01T00:00:00Z"}]"#,
    );
    let alert = |grace: &str| {
        wait(
            dir.path(),
            &bin,
            &["gate-alert", "--pr", "9", "--grace", grace],
        )
    };
    let late = alert("900");
    assert_eq!(late.status.code(), Some(1), "{late:?}");
    let text = String::from_utf8(late.stdout).unwrap();
    assert!(
        text.contains("ALERT green-without-go pr=9 head=abc run=9"),
        "{text}"
    );
    // The same run inside a huge grace is not stale yet.
    assert_eq!(alert("4000000000").status.code(), Some(0));
    fs::write(
        dir.path().join("comments.json"),
        r#"{"comments":[{"body":"GO-REQ #9 owner/proj head abc; diff stat: 1 file"}]}"#,
    )
    .unwrap();
    assert_eq!(
        alert("900").status.code(),
        Some(0),
        "a GO request suppresses it"
    );
}

#[test]
fn a_red_run_exits_one_and_usage_errors_exit_two() {
    let dir = tempfile::tempdir().unwrap();
    let bin = fake_gh(
        dir.path(),
        r#"[{"databaseId":8,"status":"completed","conclusion":"failure","headSha":"abc"}]"#,
    );
    assert_eq!(
        wait(
            dir.path(),
            &bin,
            &["ci", "--branch", "b", "--deadline", "5"]
        )
        .status
        .code(),
        Some(1)
    );
    assert_eq!(
        wait(dir.path(), &bin, &["ci", "--deadline", "5"])
            .status
            .code(),
        Some(2)
    );
    assert_eq!(
        wait(
            dir.path(),
            &bin,
            &["ci", "--branch", "b", "--deadline", "0"]
        )
        .status
        .code(),
        Some(2)
    );
}

#[test]
fn files_wait_ends_on_a_finished_status_file() {
    let dir = tempfile::tempdir().unwrap();
    let status = dir.path().join("w.status");
    fs::write(&status, "DONE k abc\nw DONE\nHEAD: x\n").unwrap();
    let bin = dir.path().join("none");
    let out = wait(
        dir.path(),
        &bin,
        &[
            "files",
            "--status",
            status.to_str().unwrap(),
            "--deadline",
            "5",
            "--poll",
            "1",
        ],
    );
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    assert!(dir.path().join("job/waits/files-w.status.json").is_file());
}

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(dir)
        .args(args)
        .env("GIT_AUTHOR_NAME", "F")
        .env("GIT_AUTHOR_EMAIL", "f@example.test")
        .env("GIT_COMMITTER_NAME", "F")
        .env("GIT_COMMITTER_EMAIL", "f@example.test")
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).unwrap().trim().into()
}

#[test]
fn a_done_status_whose_head_is_not_on_the_remote_raises_the_alert() {
    let dir = tempfile::tempdir().unwrap();
    let remote = dir.path().join("remote.git");
    let repo = dir.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    git(
        dir.path(),
        &["init", "-q", "--bare", remote.to_str().unwrap()],
    );
    git(&repo, &["init", "-q", "-b", "main"]);
    git(
        &repo,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&repo, &["commit", "--allow-empty", "-qm", "one"]);
    let head = git(&repo, &["rev-parse", "HEAD"]);
    let status = dir.path().join("w.status");
    fs::write(&status, format!("w DONE\nHEAD: {head}\n")).unwrap();
    let alert = |grace: &str| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .current_dir(&repo)
            .args([
                "wait",
                "lane-alert",
                "--status",
                status.to_str().unwrap(),
                "--grace",
                grace,
            ])
            .output()
            .unwrap()
    };
    std::thread::sleep(std::time::Duration::from_millis(1100));
    let late = alert("0");
    assert_eq!(late.status.code(), Some(1), "{late:?}");
    assert!(String::from_utf8_lossy(&late.stdout)
        .contains(&format!("ALERT lane-head-not-on-remote name=w head={head}")));
    assert_eq!(alert("3600").status.code(), Some(0), "inside the grace");
    git(&repo, &["push", "-q", "origin", "HEAD:refs/heads/lane/w"]);
    assert_eq!(alert("0").status.code(), Some(0), "pushed head");
}
