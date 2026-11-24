#![cfg(target_os = "linux")]
//! Job worktrees outside the configured roots: merged, clean and idle ones go; nothing else.
use pg_worker_core::disk_hygiene::{
    sweep_stray_worktrees, sweep_stray_worktrees_with, Guards, Report,
};
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

/// A sweep whose protection check reads the state directory fresh, as the binary's does. The
/// run record is written only after the sweep has said it is about to ask for the lock, which
/// the test holds: the checks that came before the lock cannot have seen it.
fn sweep_waiting_for_the_lock(
    repo: &std::path::Path,
    state: &std::path::Path,
    pins: &std::path::Path,
    record_run: Option<&std::path::Path>,
) -> Report {
    let lock = pg_worker_core::worktree::mutation_lock(repo).unwrap();
    let (reached_tx, reached_rx) = std::sync::mpsc::channel();
    let (repo_owned, state_owned, pins_owned) =
        (repo.to_owned(), state.to_owned(), pins.to_owned());
    let sweeper = std::thread::spawn(move || {
        let mut report = report();
        let now = SystemTime::now() + Duration::from_secs(90000);
        sweep_stray_worktrees_with(
            &repo_owned,
            &[],
            &Guards::default(),
            now,
            true,
            &mut report,
            |tree| Ok(!Guards::load(&pins_owned, &state_owned)?.protects(tree)),
            &|| {
                let _ = reached_tx.send(());
            },
        )
        .unwrap();
        report
    });
    reached_rx
        .recv_timeout(Duration::from_secs(60))
        .expect("the sweep reached the lock step");
    if let Some(tree) = record_run {
        let dir = state.join("runs/run-late");
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("record.json"),
            serde_json::json!({"status": "running", "cwd": tree}).to_string(),
        )
        .unwrap();
    }
    drop(lock);
    sweeper.join().unwrap()
}

#[test]
fn a_run_recorded_while_the_sweep_waits_for_the_lock_keeps_the_checkout() {
    for (late_run, survives) in [(true, true), (false, false)] {
        let (tmp, repo, tree) = merged_tree();
        let state = tmp.path().join("state");
        fs::create_dir_all(state.join("runs")).unwrap();
        let pins = tmp.path().join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let report =
            sweep_waiting_for_the_lock(&repo, &state, &pins, late_run.then_some(tree.as_path()));
        assert_eq!(tree.exists(), survives, "late run {late_run}: {report:?}");
    }
}

#[test]
fn a_protection_that_refuses_keeps_the_checkout() {
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
