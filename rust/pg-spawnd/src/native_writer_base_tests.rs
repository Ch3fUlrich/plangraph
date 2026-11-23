use super::*;

fn git_in(dir: &Path, args: &[&str]) -> String {
    output(
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .args([
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
            ])
            .args(args),
    )
    .unwrap()
}

/// A host over a clone whose `origin` has only `main`; `lane/local` exists in the clone alone.
fn local_base_fixture(dir: &Path) -> Host {
    let origin = dir.join("origin.git");
    let repo = dir.join("repo");
    git_in(dir, &["init", "-q", "--bare", "-b", "main", "origin.git"]);
    git_in(dir, &["clone", "-q", origin.to_str().unwrap(), "repo"]);
    git_in(&repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git_in(&repo, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    git_in(&repo, &["checkout", "-q", "-b", "lane/local"]);
    git_in(
        &repo,
        &["commit", "-q", "--allow-empty", "-m", "unpushed lane head"],
    );
    Host {
        repo,
        run_id: "run-local-base".into(),
        cwd: dir.join("worktrees/run-local-base"),
        reviews: dir.join("reviews"),
        record_path: dir.join("record.json"),
        base_ref: "refs/heads/lane/local".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.to_path_buf(),
        brief: "brief".into(),
        push_ref: Some("refs/heads/phase/local-result".into()),
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    }
}

#[cfg(unix)]
#[test]
fn host_reaps_a_writer_when_disk_crosses_the_stop_floor() {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    for (name, script) in [
        ("codex", "#!/bin/sh\necho $$ > writer.pid\nexec sleep 4\n"),
        (
            "df",
            "#!/bin/sh\nif [ -s \"$3/writer.pid\" ]; then echo 'x x x 0'; else echo 'x x x 10485760'; fi\n",
        ),
    ] {
        let path = dir.path().join(name);
        fs::write(&path, script).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let path = std::env::var_os("PATH").unwrap();
    host.tool_path = std::env::join_paths(
        std::iter::once(dir.path().to_path_buf()).chain(std::env::split_paths(&path)),
    )
    .unwrap()
    .into_string()
    .unwrap();

    assert_eq!(
        host.execute(None, "brief").unwrap_err(),
        "disk_low: under 2 GB stop floor"
    );
    let pid = fs::read_to_string(host.cwd.join("writer.pid")).unwrap();
    let pid: i32 = pid.trim().parse().unwrap();
    assert_ne!(unsafe { libc::kill(pid, 0) }, 0, "writer was not reaped");
}

#[test]
fn commit_range_counts_a_cherry_pick_and_excludes_base_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    let main = git_in(&host.cwd, &["rev-parse", "main"]);
    let base = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-qb", "old-lane", &main]);
    git_in(&host.cwd, &["commit", "-qm", "work", "--allow-empty"]);
    let original = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "lane/local"]);
    git_in(&host.cwd, &["cherry-pick", "--allow-empty", &original]);
    let picked = git_in(&host.cwd, &["rev-parse", "HEAD"]);

    assert_ne!(original, picked);
    assert!(host.has_commits_beyond(&base, &picked).unwrap());
    assert!(!host.has_commits_beyond(&base, &base).unwrap());
    assert!(!host.has_commits_beyond(&base, &main).unwrap());
}

#[test]
fn base_ref_that_exists_only_in_the_host_repo_starts_and_pushes_its_result() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), local_head);
    git_in(
        &host.cwd,
        &["commit", "-q", "--allow-empty", "-m", "writer result"],
    );
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    assert_eq!(
        host.push(&head).unwrap().as_deref(),
        Some("refs/heads/phase/local-result")
    );
    let origin = dir.path().join("origin.git");
    assert_eq!(
        git_in(&origin, &["rev-parse", "refs/heads/phase/local-result"]),
        head
    );
}

#[test]
fn base_ref_on_neither_remote_nor_host_repo_is_refused_by_name_before_a_worktree() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.base_ref = "refs/heads/lane/missing".into();
    let error = host.prepare().unwrap_err();
    assert!(error.contains("refs/heads/lane/missing"), "{error}");
    assert!(!host.cwd.exists());
}

#[test]
fn a_different_ref_that_only_contains_the_name_is_never_the_base() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    // Local `refs/heads/refs/heads/missing` would answer `rev-parse refs/heads/missing`.
    git_in(
        &host.repo,
        &[
            "update-ref",
            "refs/heads/refs/heads/missing",
            "refs/heads/lane/local",
        ],
    );
    host.base_ref = "refs/heads/missing".into();
    let error = host.prepare().unwrap_err();
    assert!(error.contains("refs/heads/missing"), "{error}");
    assert!(!host.cwd.exists());
}

#[test]
fn a_longer_remote_ref_does_not_hide_the_host_local_base() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    // `ls-remote origin refs/heads/lane/local` also lists `refs/heads/other/refs/heads/lane/local`.
    git_in(
        &host.repo,
        &[
            "push",
            "-q",
            "origin",
            "refs/heads/main:refs/heads/other/refs/heads/lane/local",
        ],
    );
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
}

#[test]
fn a_base_ref_ending_in_a_unicode_space_is_matched_byte_for_byte() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let name = "refs/heads/lane/nbsp\u{a0}";
    git_in(&host.repo, &["update-ref", name, "refs/heads/lane/local"]);
    host.base_ref = name.into();
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
}

#[test]
fn native_creation_waits_for_the_reaper_lock() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let held = pg_worker_core::worktree::mutation_lock(&host.repo).unwrap();
    let error = host.prepare().unwrap_err();
    assert!(
        error.contains("lock not acquired before the deadline"),
        "{error}"
    );
    assert!(!host.cwd.exists());
    drop(held);
    host.prepare().unwrap();
    assert!(host.cwd.exists());
}
