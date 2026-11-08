use super::*;
use std::collections::BTreeMap;

fn g(root: &Path, args: &[&str]) -> String {
    let out = git_command(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "tester")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "tester")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
struct Fixture {
    temp: tempfile::TempDir,
    source: RepoEntry,
    receiver: RepoEntry,
    base: String,
    head: String,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        g(root, &["init", "--bare", "--initial-branch=main", "origin"]);
        g(root, &["clone", "origin", "source"]);
        let source = root.join("source");
        g(&source, &["commit", "--allow-empty", "-m", "initial"]);
        g(&source, &["push", "origin", "HEAD:main"]);
        g(&source, &["remote", "set-head", "origin", "main"]);
        let base = g(&source, &["rev-parse", "HEAD"]);
        g(root, &["clone", "origin", "receiver"]);
        g(&source, &["commit", "--allow-empty", "-m", "unpushed base"]);
        g(&source, &["commit", "--allow-empty", "-m", "unpushed head"]);
        let head = g(&source, &["rev-parse", "HEAD"]);
        let entry = |name: &str| RepoEntry {
            root: root.join(name).to_str().unwrap().into(),
            remote: g(&root.join(name), &["remote", "get-url", "--all", "origin"]),
            worktrees_root: String::new(),
            python: None,
            import_roots: vec![],
            cache_env: BTreeMap::new(),
        };
        let (source, receiver) = (entry("source"), entry("receiver"));
        Self {
            temp,
            source,
            receiver,
            base,
            head,
        }
    }
    fn bundle(&self) -> std::path::PathBuf {
        self.temp.path().join("history.bundle")
    }
    fn export(&self) {
        export_bundle(&self.source, &self.head, "test-run", &self.bundle()).unwrap();
    }
    fn no_tip(&self) {
        assert!(g(
            Path::new(&self.source.root),
            &["for-each-ref", "refs/spawner/bundle"]
        )
        .is_empty());
    }
}

#[test]
fn unpushed_base_and_head_reach_clean_target() {
    let f = Fixture::new();
    let root = Path::new(&f.receiver.root);
    let refs = g(root, &["show-ref"]);
    let origin = g(&f.temp.path().join("origin"), &["show-ref"]);
    f.export();
    import_bundle(&f.receiver, &f.head, &f.bundle()).unwrap();
    g(root, &["cat-file", "-e", &format!("{}^", f.head)]);
    g(root, &["cat-file", "-e", &f.head]);
    assert_eq!(refs, g(root, &["show-ref"]));
    assert_eq!(origin, g(&f.temp.path().join("origin"), &["show-ref"]));
    f.no_tip();
}

#[test]
fn prerequisite_is_origin_merge_base_not_unpushed_base() {
    let f = Fixture::new();
    f.export();
    let bytes = fs::read(f.bundle()).unwrap();
    assert!(bytes
        .windows(41)
        .any(|w| w == format!("-{}", f.base).as_bytes()));
}

#[test]
fn verify_runs_after_origin_fetch() {
    let mut f = Fixture::new();
    let root = Path::new(&f.source.root);
    g(root, &["push", "origin", "HEAD:main"]);
    f.base = f.head;
    g(root, &["commit", "--allow-empty", "-m", "later"]);
    f.head = g(root, &["rev-parse", "HEAD"]);
    f.export();
    import_bundle(&f.receiver, &f.head, &f.bundle()).unwrap();
}

#[test]
fn temp_ref_deleted_on_success_and_failure() {
    let f = Fixture::new();
    f.export();
    f.no_tip();
    assert!(export_bundle(&f.source, &f.head, "test-run", &f.bundle()).is_err());
    f.no_tip();
    assert!(export_bundle(
        &f.source,
        &f.head,
        "test-run",
        &f.temp.path().join("absent/out")
    )
    .is_err());
    f.no_tip();
    import_bundle(&f.receiver, &f.head, &f.bundle()).unwrap();
}

#[test]
fn existing_temp_ref_never_overwritten() {
    let f = Fixture::new();
    let root = Path::new(&f.source.root);
    g(
        root,
        &["update-ref", "refs/spawner/bundle/test-run", &f.base],
    );
    assert!(export_bundle(&f.source, &f.head, "test-run", &f.bundle()).is_err());
    assert_eq!(
        f.base,
        g(root, &["rev-parse", "refs/spawner/bundle/test-run"])
    );
    assert!(!f.bundle().exists());
}

#[test]
fn run_id_traversal_and_option_refused() {
    let f = Fixture::new();
    for id in ["", "../escape", "--option/evil", "a.b", &"a".repeat(81)] {
        assert!(matches!(
            export_bundle(&f.source, &f.head, id, &f.bundle()),
            Err(ResolveError::Invalid("run_id"))
        ));
    }
    for revision in ["--help", "HEAD", &"A".repeat(40)] {
        assert!(matches!(
            export_bundle(&f.source, revision, "run", &f.bundle()),
            Err(ResolveError::Invalid("sha"))
        ));
    }
}

#[test]
fn corrupt_bundle_rejected() {
    let f = Fixture::new();
    fs::write(f.bundle(), b"not a bundle").unwrap();
    assert!(import_bundle(&f.receiver, &f.head, &f.bundle()).is_err());
}

#[test]
fn missing_prerequisite_rejected() {
    let f = Fixture::new();
    let root = Path::new(&f.source.root);
    let parent = g(root, &["rev-parse", "HEAD^"]);
    g(
        root,
        &["update-ref", "refs/spawner/bundle/test-run", &f.head],
    );
    g(
        root,
        &[
            "bundle",
            "create",
            f.bundle().to_str().unwrap(),
            "refs/spawner/bundle/test-run",
            &format!("^{parent}"),
        ],
    );
    assert!(import_bundle(&f.receiver, &f.head, &f.bundle()).is_err());
}

#[test]
fn unexpected_tip_rejected() {
    let f = Fixture::new();
    let root = Path::new(&f.source.root);
    g(
        root,
        &[
            "bundle",
            "create",
            f.bundle().to_str().unwrap(),
            "main",
            &format!("^{}", f.base),
        ],
    );
    assert!(matches!(
        import_bundle(&f.receiver, &f.head, &f.bundle()),
        Err(ResolveError::Invalid("bundle_heads"))
    ));
}

#[test]
fn oversized_and_symlink_bundle_refused() {
    let f = Fixture::new();
    fs::File::create(f.bundle())
        .unwrap()
        .set_len(64 * 1024 * 1024 + 1)
        .unwrap();
    assert!(matches!(
        import_bundle(&f.receiver, &f.head, &f.bundle()),
        Err(ResolveError::BundleTooLarge)
    ));
    #[cfg(unix)]
    {
        let link = f.temp.path().join("link");
        std::os::unix::fs::symlink(f.bundle(), &link).unwrap();
        assert!(matches!(
            import_bundle(&f.receiver, &f.head, &link),
            Err(ResolveError::Invalid("bundle_file"))
        ));
    }
}

#[test]
fn unrelated_history_and_missing_default_refused() {
    let f = Fixture::new();
    let root = Path::new(&f.source.root);
    g(
        root,
        &["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
    );
    g(
        &f.temp.path().join("origin"),
        &["symbolic-ref", "HEAD", "refs/heads/absent"],
    );
    assert!(export_bundle(&f.source, &f.head, "run", &f.bundle()).is_err());
    g(root, &["remote", "set-head", "origin", "main"]);
    g(root, &["checkout", "--orphan", "unrelated"]);
    g(root, &["commit", "--allow-empty", "-m", "unrelated"]);
    let sha = g(root, &["rev-parse", "HEAD"]);
    assert!(export_bundle(&f.source, &sha, "run", &f.bundle()).is_err());
    f.no_tip();
}

#[test]
fn empty_bundle_is_explicit() {
    let f = Fixture::new();
    assert!(matches!(
        export_bundle(&f.source, &f.base, "run", &f.bundle()),
        Err(ResolveError::Invalid("empty_bundle"))
    ));
    f.no_tip();
}

#[test]
fn no_public_ref_changes_or_push() {
    let f = Fixture::new();
    let source = Path::new(&f.source.root);
    let origin = f.temp.path().join("origin");
    let source_refs = g(source, &["show-ref"]);
    let origin_refs = g(&origin, &["show-ref"]);
    let head = g(source, &["rev-parse", "HEAD"]);
    f.export();
    assert_eq!(source_refs, g(source, &["show-ref"]));
    assert_eq!(origin_refs, g(&origin, &["show-ref"]));
    assert_eq!(head, g(source, &["rev-parse", "HEAD"]));
    f.no_tip();
}

#[test]
fn cleanup_failure_removes_complete_output() {
    let f = Fixture::new();
    let result = export_with_cleanup(&f.source, &f.head, "test-run", &f.bundle(), |root, args| {
        assert_eq!(args[0], OsStr::new("update-ref"));
        assert_eq!(args[1], OsStr::new("-d"));
        g(root, &["bundle", "verify", f.bundle().to_str().unwrap()]);
        g(
            root,
            &["update-ref", "refs/spawner/bundle/test-run", &f.base],
        );
        git(root, args)
    });
    assert!(matches!(result, Err(ResolveError::Git)));
    assert!(!f.bundle().exists());
    // A failed CAS must not be retried without its expected old value.
    assert_eq!(
        f.base,
        g(
            Path::new(&f.source.root),
            &["rev-parse", "refs/spawner/bundle/test-run"]
        )
    );
}
