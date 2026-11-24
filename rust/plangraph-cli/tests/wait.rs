//! `plangraph wait` against a fake `gh` on PATH and real git remotes.
use std::{fs, path::Path, process::Command};

/// A `gh` that answers `run list` and `pr view` and fails on anything else, logging every call.
fn fake_gh(dir: &Path, runs: &str) -> std::path::PathBuf {
    let bin = dir.join("bin");
    fs::create_dir_all(&bin).unwrap();
    fs::write(dir.join("runs.json"), runs).unwrap();
    let script = format!(
        "#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1 $2\" in\n  \"run list\") cat {runs} ;;\n  \"pr view\") echo '{{\"headRefName\":\"lane/x\",\"headRefOid\":\"abc\"}}' ;;\n  *) exit 9 ;;\nesac\n",
        log = dir.join("gh.log").display(),
        runs = dir.join("runs.json").display()
    );
    fs::write(bin.join("gh"), script).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(bin.join("gh"), fs::Permissions::from_mode(0o755)).unwrap();
    }
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
