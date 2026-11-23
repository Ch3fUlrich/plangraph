use std::io::Write;
use std::process::{Command, Stdio};

fn git(repo: &std::path::Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    String::from_utf8(output.stdout).unwrap().trim().into()
}

#[test]
fn part_description_flags_every_key_missing_a_commit_trailer() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(
        repo,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            "base\n\nPlan-Item: demo:LE-B",
        ],
    );
    let base = git(repo, &["rev-parse", "HEAD"]);
    let check = |message: &str| {
        git(repo, &["commit", "--allow-empty", "-qm", message]);
        let mut child = Command::new(env!("CARGO_BIN_EXE_pg-policy"))
            .current_dir(repo)
            .args(["--plan-item-trailers", &base, "HEAD"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(b"Part fixes demo:LE-A and `demo:LE-B`; parent demo:PHASE. demo:LE-B. Mixed.Project:lower.key.")
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{:?}", output);
        String::from_utf8(output.stdout).unwrap()
    };
    let missing = check(
        "fix under parent\n\nCloses: demo:LE-B\n\nPlan-Item: demo:LE-A\nPlan-Item: demo:PHASE",
    );
    assert!(missing.contains("demo:LE-B"), "{missing}");
    assert!(!missing.contains("demo:LE-A"), "{missing}");
    assert!(!missing.contains("demo:PHASE"), "{missing}");
    assert_eq!(missing.matches("demo:LE-B").count(), 1);
    assert!(missing.contains("Mixed.Project:lower.key"));
    assert_eq!(
        check("fix both\n\nPlan-Item: demo:LE-A\nPlan-Item: demo:LE-B\nPlan-Item: Mixed.Project:lower.key"),
        ""
    );
}

#[test]
fn merge_only_and_prose_trailers_do_not_cover_a_key() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "--allow-empty", "-qm", "base"]);
    let base = git(repo, &["rev-parse", "HEAD"]);
    git(repo, &["checkout", "-qb", "part"]);
    git(
        repo,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            "fix\n\nPlan-Item: demo:LE-A\n\nMore prose, not a trailer.",
        ],
    );
    git(repo, &["checkout", "-q", "main"]);
    git(
        repo,
        &[
            "merge",
            "--no-ff",
            "-qm",
            "merge\n\nPlan-Item: demo:LE-A",
            "part",
        ],
    );
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .current_dir(repo)
        .args(["--plan-item-trailers", &base, "HEAD"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"Fix demo:LE-A")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("demo:LE-A"));
    let invalid = Command::new(env!("CARGO_BIN_EXE_pg-policy"))
        .current_dir(repo)
        .args(["--plan-item-trailers", "missing-base", "HEAD"])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!invalid.status.success());
}

#[test]
fn review_checklist_reports_fix_commits_without_a_class_search() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    let check = |body: &str| {
        git(repo, &["commit", "--allow-empty", "-qm", body]);
        Command::new(env!("CARGO_BIN_EXE_pg-policy"))
            .current_dir(repo)
            .args(["--class-search", "HEAD"])
            .output()
            .unwrap()
    };
    for body in [
        "Fix a failed test",
        "Fix a failed test\n\nClass-Search: cargo -> 1 hits, 2 changed",
    ] {
        let output = check(body);
        let sha = git(repo, &["rev-parse", "HEAD"]);
        let diagnostic = String::from_utf8(output.stdout).unwrap();
        assert!(!output.status.success());
        assert!(
            diagnostic.contains(&format!("{sha}: missing or invalid Class-Search")),
            "{diagnostic}"
        );
    }
    let output = check(
        "Fix a failed test\n\nClass-Search: spawn_cargo -> 2 hits, 2 changed\n\nPlan-Item: demo:FIX",
    );
    assert!(output.status.success(), "{output:?}");
}

#[test]
fn class_search_accepts_a_record_from_a_later_commit_on_the_branch() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "--allow-empty", "-qm", "Fix fixture"]);
    let sha = git(repo, &["rev-parse", "HEAD"]);
    let check = || {
        Command::new(env!("CARGO_BIN_EXE_pg-policy"))
            .current_dir(repo)
            .args(["--class-search", &sha])
            .output()
            .unwrap()
    };
    assert!(!check().status.success());
    for record in [
        "00000000 fixture -> 2 hits, 1 changed".into(),
        format!("{} fixture -> 1 hits, 2 changed", &sha[..8]),
    ] {
        git(
            repo,
            &[
                "commit",
                "--allow-empty",
                "-qm",
                &format!("Audit\n\nClass-Search: {record}"),
            ],
        );
        assert!(!check().status.success());
    }
    git(
        repo,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            &format!(
                "Audit fixture fixes\n\nClass-Search: {} fixture -> 2 hits, 1 changed",
                &sha[..8]
            ),
        ],
    );
    let output = check();
    assert!(output.status.success(), "{output:?}");
    git(repo, &["checkout", "-qb", "other", &sha]);
    assert!(
        !check().status.success(),
        "a record on a different branch counted"
    );
}

#[test]
fn class_search_exempts_resolutions_during_train_delta_review() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "--allow-empty", "-qm", "Fixture base"]);
    let check = |flags: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_pg-policy"))
            .current_dir(repo)
            .arg("--class-search")
            .args(flags)
            .arg("HEAD")
            .output()
            .unwrap()
    };
    git(
        repo,
        &[
            "commit",
            "--allow-empty",
            "-qm",
            "Train 12: resolve fixture",
        ],
    );
    assert!(!check(&[]).status.success());
    let output = check(&["--train-delta"]);
    assert!(output.status.success(), "{output:?}");
    for subject in [
        "Train 12: drop fixture",
        "Train 12: resolved fixture",
        "Fix fixture",
    ] {
        git(repo, &["commit", "--allow-empty", "-qm", subject]);
        assert!(!check(&["--train-delta"]).status.success(), "{subject}");
    }
}
