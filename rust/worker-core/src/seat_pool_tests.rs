use super::*;
#[test]
fn rejects_injection_and_missing_grants() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = RepoEntry {
        root: tmp.path().to_string_lossy().into(),
        worktrees_root: tmp.path().to_string_lossy().into(),
        remote: "unused".into(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    let mut ws = Workspace {
        repo_id: "fixture".into(),
        mode: Mode::Read,
        base_sha: None,
        revision_sha: Some("a".repeat(40)),
        branch: None,
    };
    assert!(prepare_seat(&repo, &ws, tmp.path(), "run", 1).is_err());
    assert!(prepare_seat(&repo, &ws, tmp.path(), "../run", 1).is_err());
    ws.revision_sha = Some("--all".into());
    assert!(prepare_seat(&repo, &ws, tmp.path(), "run", 1).is_err());
}
