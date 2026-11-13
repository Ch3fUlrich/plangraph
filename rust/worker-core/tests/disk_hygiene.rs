use pg_worker_core::{
    disk_hygiene::{sweep, Guards},
    host_command,
};
use std::{
    fs,
    path::Path,
    process::Command,
    time::{Duration, SystemTime},
};
fn git(repo: &Path, args: &[&str]) {
    host_command::output(
        Command::new("git")
            .current_dir(repo)
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
fn fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("main");
    let root = tmp.path().join("trees");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&root).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join(".gitignore"), "target/\nnode_modules/\n").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "one"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    for name in ["pinned", "dirty", "live", "clean"] {
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                name,
                root.join(name).to_str().unwrap(),
            ],
        );
        fs::create_dir(root.join(name).join("target")).unwrap();
        fs::write(root.join(name).join("target/cache"), [0u8; 64]).unwrap();
    }
    fs::write(root.join("dirty/untracked"), "valuable").unwrap();
    (tmp, repo, root)
}
#[test]
fn pinned_dirty_and_live_survive_real_cleanup() {
    let (_tmp, repo, root) = fixture();
    let guards = Guards {
        pinned: vec![root.join("pinned")],
        live: vec![root.join("live")],
    };
    let report = sweep(
        &repo,
        &root,
        &guards,
        SystemTime::now() + Duration::from_secs(90000),
        85.0,
        true,
    )
    .unwrap();
    for name in ["pinned", "dirty", "live"] {
        assert!(root.join(name).join("target/cache").exists());
    }
    assert!(!root.join("clean").exists());
    assert!(report.alarm && report.freed_bytes >= 64);
    println!("cleanup freed_bytes={}", report.freed_bytes);
}
#[test]
fn dry_run_and_recent_worktrees_preserve_everything() {
    let (_tmp, repo, root) = fixture();
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        85.0,
        false,
    )
    .unwrap();
    assert!(!report.actions.is_empty() && report.freed_bytes == 0);
    assert!(root.join("clean/target/cache").exists());
    let recent = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now(),
        85.0,
        true,
    )
    .unwrap();
    assert!(recent.actions.is_empty() && root.join("clean/target/cache").exists());
    let low = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        79.0,
        true,
    )
    .unwrap();
    assert!(low.alarm && low.actions.is_empty());
}

#[test]
fn unmerged_local_branch_keeps_worktree_but_reclaims_ignored_cache() {
    let (_tmp, repo, root) = fixture();
    let clean = root.join("clean");
    fs::write(clean.join("valuable"), "unmerged").unwrap();
    git(&clean, &["add", "valuable"]);
    git(&clean, &["commit", "-qm", "local"]);
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        85.0,
        true,
    )
    .unwrap();
    assert!(clean.join("valuable").exists() && !clean.join("target").exists());
    assert!(report.freed_bytes >= 64);
}

#[test]
fn missing_or_malformed_pin_and_live_state_abort() {
    let tmp = tempfile::tempdir().unwrap();
    let pins = tmp.path().join("pins.json");
    assert!(Guards::load(&pins, tmp.path()).is_err());
    fs::write(&pins, "[]").unwrap();
    fs::create_dir_all(tmp.path().join("runs/run-active")).unwrap();
    fs::write(
        tmp.path().join("runs/run-active/record.json"),
        "{\"status\":\"running\"}",
    )
    .unwrap();
    assert!(Guards::load(&pins, tmp.path()).is_err());
}
#[test]
#[cfg(unix)]
fn symlinked_caches_never_touch_external_data() {
    let (_tmp, repo, root) = fixture();
    let outside = tempfile::tempdir().unwrap();
    fs::write(outside.path().join("keep"), "keep").unwrap();
    fs::remove_file(root.join("clean/target/cache")).unwrap();
    fs::remove_dir(root.join("clean/target")).unwrap();
    std::os::unix::fs::symlink(outside.path(), root.join("clean/target")).unwrap();
    let _ = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        85.0,
        true,
    );
    assert!(outside.path().join("keep").exists());
    assert!(root.join("clean").exists());
}
