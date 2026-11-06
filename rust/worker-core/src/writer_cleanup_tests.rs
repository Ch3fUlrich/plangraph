use super::*;
#[test]
fn dry_run_no_prune() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = RepoEntry {
        root: tmp.path().to_string_lossy().into(),
        worktrees_root: tmp.path().to_string_lossy().into(),
        remote: "unused".into(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    assert!(janitor(&repo, tmp.path(), 90000, false).unwrap().is_empty());
    assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
}
#[test]
fn seal_without_metadata_is_not_recorded() {
    let tmp = tempfile::tempdir().unwrap();
    let blob = LocalBlobStore::open(&tmp.path().join("blobs"))
        .unwrap()
        .seal(b"fixture")
        .unwrap();
    assert!(record_result(tmp.path(), "run", &blob).is_err());
    assert!(!tmp.path().join("writer-lifecycle/run.receipt").exists());
}
