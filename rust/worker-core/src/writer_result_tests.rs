use super::*;
#[test]
fn option_oid_ref_traversal_refused() {
    for value in ["--help", "../escape", "refs/heads/main", "HEAD"] {
        assert!(!oid(value));
    }
    assert_eq!(
        WriterError::SecretInHistory.to_string(),
        "secret_in_history"
    );
}

struct Fixture {
    _dir: tempfile::TempDir,
    repo: RepoEntry,
    ws: Workspace,
    tree: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("repo");
        let trees = dir.path().join("trees");
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(&trees).unwrap();
        git(&root, &["init", "-b", "main"]).unwrap();
        git(&root, &["config", "user.name", "Test"]).unwrap();
        git(&root, &["config", "user.email", "test@example.org"]).unwrap();
        git(&root, &["commit", "--allow-empty", "-m", "initial"]).unwrap();
        let base = git(&root, &["rev-parse", "HEAD"]).unwrap();
        git(&root, &["remote", "add", "origin", root.to_str().unwrap()]).unwrap();
        git(&root, &["update-ref", "refs/remotes/origin/main", &base]).unwrap();
        git(
            &root,
            &[
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            ],
        )
        .unwrap();
        let repo = RepoEntry {
            root: root.to_str().unwrap().into(),
            worktrees_root: trees.to_str().unwrap().into(),
            remote: root.to_str().unwrap().into(),
            python: None,
            import_roots: vec![],
            cache_env: Default::default(),
        };
        let ws = Workspace {
            repo_id: "fixture".into(),
            mode: Mode::Writer,
            base_sha: Some(base.clone()),
            revision_sha: Some(base),
            branch: Some("lane/result".into()),
        };
        let tree = crate::workspace_launch::create_writer(&repo, &ws, "writer").unwrap();
        Self {
            _dir: dir,
            repo,
            ws,
            tree,
        }
    }
    fn commit(&self, message: &str) {
        git(&self.tree, &["commit", "--allow-empty", "-m", message]).unwrap();
    }
}
struct Gate(bool);
#[test]
fn descendant_all_new_trailers_including_merge() {
    let f = Fixture::new();
    f.commit("first\n\nWriter: test");
    git(&f.tree, &["checkout", "-b", "lane/side"]).unwrap();
    f.commit("side\n\nWriter: test");
    git(&f.tree, &["checkout", "lane/result"]).unwrap();
    f.commit("main\n\nWriter: test");
    git(
        &f.tree,
        &[
            "merge",
            "--no-ff",
            "lane/side",
            "-m",
            "merge without trailer",
        ],
    )
    .unwrap();
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
}
#[test]
fn temp_ref_cleanup_only_own_ref() {
    let f = Fixture::new();
    f.commit("one\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    git(
        &f.tree,
        &[
            "update-ref",
            "refs/spawner/bundle/export",
            &checked.base_sha,
        ],
    )
    .unwrap();
    let mut store = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    assert!(export_result(&f.repo, &checked, "export", &Gate(true), &mut store).is_err());
    assert_eq!(
        git(&f.tree, &["rev-parse", "refs/spawner/bundle/export"]).unwrap(),
        checked.base_sha
    );
}
impl ContentGate for Gate {
    fn check(&self, _: &RepoEntry, _: &str, _: &str) -> Result<(), WriterError> {
        if self.0 {
            Ok(())
        } else {
            Err(WriterError::SecretInHistory)
        }
    }
}
#[test]
fn body_writer_not_trailer() {
    let f = Fixture::new();
    f.commit("subject\n\nWriter: body\n\nordinary final paragraph");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
}
#[test]
fn empty_range_dirty_tree_wrong_branch_refuse() {
    let f = Fixture::new();
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
    f.commit("result\n\nWriter: test");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    std::fs::write(f.tree.join("untracked"), "dirty").unwrap();
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
    std::fs::remove_file(f.tree.join("untracked")).unwrap();
    git(&f.tree, &["checkout", "-b", "lane/wrong"]).unwrap();
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
}
#[test]
fn daemon_never_pushes() {
    let f = Fixture::new();
    f.commit("result\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    let root = Path::new(&f.repo.root);
    let before = git(root, &["rev-parse", "main"]).unwrap();
    let mut store = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    let blob = export_result(&f.repo, &checked, "export", &Gate(true), &mut store).unwrap();
    assert!(!store.read(&blob).unwrap().is_empty());
    assert_eq!(git(root, &["rev-parse", "main"]).unwrap(), before);
    assert!(f.tree.exists());
    assert!(git(root, &["for-each-ref", "refs/spawner/bundle/"])
        .unwrap()
        .is_empty());
}
#[test]
fn changed_head_refuses_export() {
    let f = Fixture::new();
    f.commit("one\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    f.commit("two\n\nWriter: test");
    let mut store = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    assert!(export_result(&f.repo, &checked, "export", &Gate(true), &mut store).is_err());
}
#[test]
fn gate_failure_creates_no_bundle() {
    let f = Fixture::new();
    f.commit("one\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    let root = f._dir.path().join("blobs");
    let mut store = crate::writer_blob::LocalBlobStore::open(&root).unwrap();
    assert!(matches!(
        export_result(&f.repo, &checked, "export", &Gate(false), &mut store),
        Err(WriterError::SecretInHistory)
    ));
    assert_eq!(std::fs::read_dir(root).unwrap().count(), 0);
}
