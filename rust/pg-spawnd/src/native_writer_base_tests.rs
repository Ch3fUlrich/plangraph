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
