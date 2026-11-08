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

#[test]
fn writer_oid_rejects_every_uppercase_hex_digit() {
    assert!(oid(&"abcdef0123456789".repeat(2).repeat(2)[..40]));
    for upper in b'A'..=b'F' {
        let mut value = "a".repeat(40).into_bytes();
        value[20] = upper;
        assert!(!oid(std::str::from_utf8(&value).unwrap()));
    }
}

#[test]
fn writer_none_launch_missing_root_stays_confined() {
    let mut f = Fixture::new();
    f.ws.mode = Mode::None;
    f.ws.base_sha = None;
    f.ws.revision_sha = None;
    f.ws.branch = None;
    let temp = f._dir.path();
    let missing = temp.join("missing/trees");
    f.repo.worktrees_root = missing.to_str().unwrap().into();
    let launch = |repo: &RepoEntry, temp: &Path| {
        crate::workspace_launch::prepare_launch(
            &mut std::process::Command::new("unused"),
            repo,
            &f.ws,
            None,
            "seat",
            temp,
            None,
        )
    };
    assert!(matches!(
        launch(&f.repo, temp),
        Err(crate::workspace_resolver::ResolveError::Verify(
            "worktrees_root"
        ))
    ));
    assert!(!missing.exists());
    assert!(!temp.join("missing").exists());
    let nested = temp.join("existing/trees");
    std::fs::create_dir_all(&nested).unwrap();
    f.repo.worktrees_root = nested.to_str().unwrap().into();
    assert!(launch(&f.repo, temp).unwrap().is_none());
    for relative in ["../escape", "missing/../escape"] {
        f.repo.worktrees_root = temp.join(relative).to_str().unwrap().into();
        assert!(launch(&f.repo, temp).is_err(), "accepted {relative}");
    }
    f.repo.worktrees_root = temp.to_str().unwrap().into();
    assert!(launch(&f.repo, temp).is_err());
    #[cfg(unix)]
    {
        let link = temp.join("trees-link");
        std::os::unix::fs::symlink(temp, &link).unwrap();
        f.repo.worktrees_root = link.to_str().unwrap().into();
        assert!(launch(&f.repo, temp).is_err());
    }
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
            live_dir: None,
            health_checks: None,
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
fn base_requires_lowercase_oid() {
    let mut f = Fixture::new();
    f.commit("result\n\nWriter: test");
    for base in [
        "refs/heads/main",
        "--all",
        "HEAD",
        "../escape",
        &"A".repeat(40),
    ] {
        f.ws.base_sha = Some(base.into());
        assert!(matches!(
            check_result(&f.repo, &f.tree, &f.ws),
            Err(WriterError::Invalid)
        ));
    }
}
fn separate_origin(f: &mut Fixture) -> PathBuf {
    let origin = f._dir.path().join("origin");
    git(
        f._dir.path(),
        &["clone", "--bare", &f.repo.root, origin.to_str().unwrap()],
    )
    .unwrap();
    f.repo.remote = origin.to_str().unwrap().into();
    git(
        Path::new(&f.repo.root),
        &["remote", "set-url", "origin", &f.repo.remote],
    )
    .unwrap();
    origin
}
#[test]
fn unpushed_base_and_head_bundle_roundtrip() {
    let mut f = Fixture::new();
    let origin = separate_origin(&mut f);
    git(&origin, &["update-ref", "-d", "refs/heads/lane/result"]).unwrap();
    f.commit("unpushed base\n\nWriter: test");
    let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    f.ws.base_sha = Some(base.clone());
    f.ws.revision_sha = Some(base.clone());
    f.commit("unpushed head\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    for sha in [&base, &checked.head_sha] {
        assert!(git(&origin, &["cat-file", "-e", sha]).is_err());
    }
    let receiver = f._dir.path().join("receiver");
    git(
        f._dir.path(),
        &[
            "clone",
            origin.to_str().unwrap(),
            receiver.to_str().unwrap(),
        ],
    )
    .unwrap();
    let mut store = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    let blob = export_result(&f.repo, &checked, "roundtrip", &Gate(true), &mut store).unwrap();
    let bundle = f._dir.path().join("result.bundle");
    std::fs::write(&bundle, store.read(&blob).unwrap()).unwrap();
    git(&receiver, &["bundle", "verify", bundle.to_str().unwrap()]).unwrap();
    git(
        &receiver,
        &[
            "fetch",
            bundle.to_str().unwrap(),
            "refs/spawner/bundle/roundtrip",
        ],
    )
    .unwrap();
    assert_eq!(
        git(&receiver, &["rev-parse", "FETCH_HEAD"]).unwrap(),
        checked.head_sha
    );
    git(
        &receiver,
        &["merge-base", "--is-ancestor", &base, &checked.head_sha],
    )
    .unwrap();
}
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
    let mut f = Fixture::new();
    let origin = separate_origin(&mut f);
    git(&origin, &["update-ref", "-d", "refs/heads/lane/result"]).unwrap();
    f.commit("result\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    let root = Path::new(&f.repo.root);
    let before = git(root, &["rev-parse", "main"]).unwrap();
    let mut store = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    let blob = export_result(&f.repo, &checked, "export", &Gate(true), &mut store).unwrap();
    assert!(!store.read(&blob).unwrap().is_empty());
    assert!(git(&origin, &["for-each-ref", "refs/heads/lane/result"])
        .unwrap()
        .is_empty());
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
#[test]
fn writer_result_oid_remains_sha1_only() {
    assert!(oid(&"a".repeat(40)));
    assert!(!oid(&"a".repeat(64)));
}
