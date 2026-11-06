use super::*;
fn git(root: &Path, args: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .current_dir(root)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", root.join("absent"))
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "git fixture failed");
    String::from_utf8_lossy(&output.stdout).trim().into()
}
#[test]
fn real_temp_git_commits_exact_diff() {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir_all(&root).unwrap();
    git(&root, &["init", "-q"]);
    git(&root, &["config", "user.name", "Fixture"]);
    git(&root, &["config", "user.email", "fixture@example.invalid"]);
    std::fs::write(root.join("file"), b"before\n").unwrap();
    git(&root, &["add", "file"]);
    git(&root, &["commit", "-qm", "before"]);
    let base = git(&root, &["rev-parse", "HEAD"]);
    std::fs::write(root.join("file"), b"after\x9d\n").unwrap();
    git(&root, &["commit", "-qam", "after"]);
    let head = git(&root, &["rev-parse", "HEAD"]);
    let repo = RepoEntry {
        root: root.to_string_lossy().into(),
        worktrees_root: root.join("trees").to_string_lossy().into(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    let diff = DiffRef {
        repo_id: "repo".into(),
        base_sha: base,
        head_sha: head,
    };
    let bytes = read_diff(&repo, &diff, 262144).unwrap();
    assert!(bytes.contains(&0x9d));
    assert!(String::from_utf8_lossy(&bytes).contains('\u{fffd}'));
    assert_eq!(read_diff(&repo, &diff, 1), Err(InlineDiffError::TooLarge));
    let mut repos = std::collections::BTreeMap::new();
    repos.insert("repo".into(), repo);
    let mut launch = crate::launch::LaunchSpec {
        argv: vec!["native".into()],
        cwd: root.clone(),
        env: Default::default(),
        stdin: Some("context".into()),
        prompt: Some("context".into()),
        warnings: None,
        events: None,
    };
    let args = serde_json::json!({"role":"review", "card":{"diff_ref":diff}});
    crate::prompt::prepare_review(&args, &serde_json::json!({}), &mut launch, &repos, true)
        .unwrap();
    assert!(launch.prompt.as_ref().unwrap().contains("after"));
    assert_eq!(launch.prompt, launch.stdin);
    crate::prompt::prepare_review(
        &args,
        &serde_json::json!({"review_mode":"inline_only"}),
        &mut launch,
        &repos,
        false,
    )
    .unwrap();
    assert!(launch.prompt.as_ref().unwrap().starts_with("NO-tools:"));
    assert_eq!(
        launch
            .prompt
            .as_ref()
            .unwrap()
            .matches("<!-- PLANGRAPH INLINE DIFF BEGIN -->")
            .count(),
        1
    );
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn ref_option_path_traversal_refused() {
    let repo = RepoEntry {
        root: ".".into(),
        worktrees_root: ".".into(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    for sha in ["--output=elsewhere", "../HEAD", "HEAD", "ABC"] {
        let diff = DiffRef {
            repo_id: "repo".into(),
            base_sha: sha.into(),
            head_sha: sha.into(),
        };
        assert_eq!(
            read_diff(&repo, &diff, 10),
            Err(InlineDiffError::InvalidDiffRef)
        );
    }
}
