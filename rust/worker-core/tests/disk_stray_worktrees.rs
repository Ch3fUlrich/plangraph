#![cfg(target_os = "linux")]
//! Job worktrees outside the configured roots: merged, clean and idle ones go; nothing else.
use pg_worker_core::disk_hygiene::{sweep_stray_worktrees, Guards, Report};
use std::{
    fs,
    path::Path,
    time::{Duration, SystemTime},
};

fn git(repo: &Path, args: &[&str]) {
    pg_worker_core::host_command::output(
        pg_worker_core::git_test_support::command(repo)
            .args([
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args),
        Duration::from_secs(10),
    )
    .unwrap();
}

fn report() -> Report {
    Report {
        free_bytes: 0,
        alarm: false,
        dry_run: false,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    }
}

#[test]
fn only_merged_clean_idle_stray_worktrees_are_removed() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("main");
    let roots = tmp.path().join("roots");
    let jobs = tmp.path().join("jobs");
    for dir in [&repo, &roots, &jobs] {
        fs::create_dir(dir).unwrap();
    }
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join("one"), "one").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "one"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    for (parent, name) in [
        (&jobs, "merged"),
        (&jobs, "dirty"),
        (&jobs, "pushed"),
        (&jobs, "pinned"),
        (&roots, "inside"),
    ] {
        let path = parent.join(name);
        git(
            &repo,
            &["worktree", "add", "-qb", name, path.to_str().unwrap()],
        );
    }
    fs::write(jobs.join("dirty/untracked"), "valuable").unwrap();
    fs::write(jobs.join("pushed/work"), "pushed but unmerged").unwrap();
    git(&jobs.join("pushed"), &["add", "work"]);
    git(&jobs.join("pushed"), &["commit", "-qm", "work"]);
    git(
        &jobs.join("pushed"),
        &["update-ref", "refs/remotes/origin/pushed", "HEAD"],
    );

    let mut report = report();
    let guards = Guards {
        pinned: vec![jobs.join("pinned").canonicalize().unwrap()],
        live: vec![],
    };
    let roots = vec![roots.canonicalize().unwrap()];
    let now = SystemTime::now() + Duration::from_secs(90000);
    sweep_stray_worktrees(&repo, &roots, &guards, now, true, &mut report, |_| Ok(true)).unwrap();

    assert!(!jobs.join("merged").exists(), "{report:?}");
    for kept in ["dirty", "pushed", "pinned"] {
        assert!(jobs.join(kept).exists(), "{kept}");
    }
    assert!(
        tmp.path().join("roots/inside").exists(),
        "a tree under a configured root is not this sweep's"
    );
    assert!(
        report
            .skipped
            .iter()
            .any(|s| s.contains("pushed commits not in origin/main")),
        "{:?}",
        report.skipped
    );
    assert!(report
        .actions
        .iter()
        .any(|a| a.contains("removed stray worktree") && a.contains("merged")));
}

fn merged_tree() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("main");
    let jobs = tmp.path().join("jobs");
    for dir in [&repo, &jobs] {
        fs::create_dir(dir).unwrap();
    }
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join("one"), "one").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "one"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    let tree = jobs.join("job");
    git(
        &repo,
        &["worktree", "add", "-qb", "job", tree.to_str().unwrap()],
    );
    (tmp, repo, tree)
}

#[test]
fn a_checkout_that_changes_while_the_lock_is_awaited_is_not_removed() {
    let now = SystemTime::now() + Duration::from_secs(90000);
    // The protection check runs under the lock, last before removal: its side effect is the race.
    for race in ["dirty", "unmerged"] {
        let (_tmp, repo, tree) = merged_tree();
        let mut report = report();
        let racing = tree.clone();
        sweep_stray_worktrees(
            &repo,
            &[],
            &Guards::default(),
            now,
            true,
            &mut report,
            |_| {
                if race == "dirty" {
                    fs::write(racing.join("new-work"), "valuable").unwrap();
                } else {
                    fs::write(racing.join("late"), "late").unwrap();
                    git(&racing, &["add", "late"]);
                    git(&racing, &["commit", "-qm", "late"]);
                }
                Ok(true)
            },
        )
        .unwrap();
        assert!(
            tree.join(if race == "dirty" { "new-work" } else { "late" })
                .exists(),
            "{race}: {report:?}"
        );
        assert_eq!(report.freed_bytes, 0, "{race}");
    }
}

#[test]
fn a_run_that_appears_before_the_final_check_keeps_the_checkout() {
    let (_tmp, repo, tree) = merged_tree();
    let mut report = report();
    let now = SystemTime::now() + Duration::from_secs(90000);
    sweep_stray_worktrees(
        &repo,
        &[],
        &Guards::default(),
        now,
        true,
        &mut report,
        |_| Ok(false),
    )
    .unwrap();
    assert!(tree.exists());
    assert!(report
        .skipped
        .iter()
        .any(|s| s.contains("active work or build")));
}
