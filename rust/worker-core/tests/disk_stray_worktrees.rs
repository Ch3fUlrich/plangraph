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
