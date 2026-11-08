use super::*;
use std::collections::BTreeMap;
use tempfile::TempDir;

fn git(root: &Path, args: &[&str]) -> String {
    let out = git_command(root)
        .env("HOME", root)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", root.join("absent-config"))
        .env("GIT_AUTHOR_NAME", "tester")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "tester")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "git failed: {args:?}");
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

struct Fixture {
    temp: TempDir,
    repo: RepoEntry,
    sha: String,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        git(root, &["init", "--bare", "origin"]);
        git(root, &["init", "source"]);
        let source = root.join("source");
        git(&source, &["commit", "--allow-empty", "-m", "initial"]);
        let sha = git(&source, &["rev-parse", "HEAD"]);
        git(root, &["clone", "--no-local", "source", "checkout"]);
        let origin = root.join("origin").to_str().unwrap().to_owned();
        git(&source, &["remote", "add", "origin", &origin]);
        git(&source, &["push", "origin", "HEAD:refs/heads/main"]);
        let checkout = root.join("checkout");
        git(&checkout, &["remote", "set-url", "origin", &origin]);
        Self {
            repo: RepoEntry {
                live_dir: None,
                root: checkout.to_str().unwrap().into(),
                remote: origin,
                worktrees_root: String::new(),
                python: None,
                import_roots: vec![],
                cache_env: BTreeMap::new(),
            },
            temp,
            sha,
        }
    }
    fn advance(&self) -> String {
        let source = self.temp.path().join("source");
        git(&source, &["commit", "--allow-empty", "-m", "next"]);
        git(&source, &["push", "origin", "HEAD:refs/heads/main"]);
        git(&source, &["rev-parse", "HEAD"])
    }
    fn lock(&self) -> PathBuf {
        common_dir(Path::new(&self.repo.root))
            .unwrap()
            .join("spawner-fetch.lock")
    }
    fn refs(&self) -> String {
        git(&self.temp.path().join("origin"), &["show-ref"])
    }
}

#[test]
fn existing_commit_never_fetches() {
    let mut f = Fixture::new();
    f.repo.remote = "unusable".into();
    std::fs::write(
        Path::new(&f.repo.root).join(".git/FETCH_HEAD"),
        "existing\n",
    )
    .unwrap();
    let guard = lockfile::try_lock(&f.lock()).unwrap().unwrap();
    ensure_commit(&f.repo, &f.sha, Duration::ZERO).unwrap();
    drop(guard);
}

#[test]
fn non_commit_objects_are_sha_not_reachable() {
    let f = Fixture::new();
    let root = Path::new(&f.repo.root);
    let blob = git(root, &["hash-object", "-w", "--stdin"]);
    let tree = git(root, &["rev-parse", "HEAD^{tree}"]);
    git(root, &["tag", "-a", "blob-tag", &blob, "-m", "blob tag"]);
    let tag = git(root, &["rev-parse", "refs/tags/blob-tag"]);
    for sha in [&blob, &tree, &tag] {
        assert!(matches!(
            ensure_commit(&f.repo, sha, Duration::ZERO),
            Err(ResolveError::ShaNotReachable)
        ));
        assert!(lockfile::try_lock(&f.lock()).unwrap().is_some());
    }
    git(
        root,
        &["tag", "-a", "commit-tag", &f.sha, "-m", "commit tag"],
    );
    let commit_tag = git(root, &["rev-parse", "refs/tags/commit-tag"]);
    ensure_commit(&f.repo, &commit_tag, Duration::ZERO).unwrap();
}

#[test]
fn missing_commit_fetches_local_origin() {
    let f = Fixture::new();
    let sha = f.advance();
    let refs = f.refs();
    ensure_commit(&f.repo, &sha, Duration::ZERO).unwrap();
    assert!(has_commit(Path::new(&f.repo.root), &sha).unwrap());
    assert_eq!(refs, f.refs());
}

#[test]
fn lock_timeout_and_error_release() {
    let mut f = Fixture::new();
    let sha = f.advance();
    let guard = lockfile::try_lock(&f.lock()).unwrap().unwrap();
    assert!(matches!(
        ensure_commit(&f.repo, &sha, Duration::ZERO),
        Err(ResolveError::Lock)
    ));
    drop(guard);
    git(
        Path::new(&f.repo.root),
        &["remote", "set-url", "origin", "absent"],
    );
    f.repo.remote = "absent".into();
    assert!(matches!(
        ensure_commit(&f.repo, &sha, Duration::ZERO),
        Err(ResolveError::Git)
    ));
    assert!(lockfile::try_lock(&f.lock()).unwrap().is_some());
}

#[test]
fn malformed_sha_cannot_be_option() {
    let f = Fixture::new();
    for sha in [
        "--help",
        "HEAD",
        &"A".repeat(40),
        &"0".repeat(39),
        "HEAD^{commit}",
    ] {
        assert!(matches!(
            ensure_commit(&f.repo, sha, Duration::ZERO),
            Err(ResolveError::Invalid(_))
        ));
    }
    assert!(!f.lock().exists());
}

#[test]
fn remote_mismatch_refuses_before_fetch() {
    let mut f = Fixture::new();
    let sha = f.advance();
    let refs = f.refs();
    f.repo.remote = "different".into();
    assert!(matches!(
        ensure_commit(&f.repo, &sha, Duration::ZERO),
        Err(ResolveError::Verify("remote"))
    ));
    assert!(!has_commit(Path::new(&f.repo.root), &sha).unwrap());
    assert!(lockfile::try_lock(&f.lock()).unwrap().is_some());
    assert_eq!(refs, f.refs());
}

#[test]
fn missing_after_fetch_is_sha_not_reachable() {
    let f = Fixture::new();
    let refs = f.refs();
    assert!(matches!(
        ensure_commit(&f.repo, &"0".repeat(40), Duration::ZERO),
        Err(ResolveError::ShaNotReachable)
    ));
    assert_eq!(refs, f.refs());
}

// Child test processes avoid mutating the parent process environment.
#[test]
fn resolver_child() {
    let Some(root) = std::env::var_os("RESOLVER_TEST_ROOT") else {
        return;
    };
    let repo = RepoEntry {
        live_dir: None,
        root: root.to_str().unwrap().into(),
        remote: "must-not-fetch".into(),
        worktrees_root: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: BTreeMap::new(),
    };
    let sha = std::env::var("RESOLVER_TEST_SHA").unwrap();
    std::fs::write(std::env::var_os("RESOLVER_TEST_READY").unwrap(), []).unwrap();
    ensure_commit(&repo, &sha, Duration::from_secs(10)).unwrap();
}

fn child(f: &Fixture, root: &Path, sha: &str, ready: &Path) -> std::process::Child {
    Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "workspace_resolver::tests::resolver_child",
            "--nocapture",
        ])
        .env("RESOLVER_TEST_ROOT", root)
        .env("RESOLVER_TEST_SHA", sha)
        .env("RESOLVER_TEST_READY", ready)
        .env("HOME", f.temp.path())
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", f.temp.path().join("absent-config"))
        .env("GIT_DIR", f.temp.path().join("not-a-repository"))
        .spawn()
        .unwrap()
}

#[test]
fn inherited_git_location_cannot_redirect_repo() {
    let f = Fixture::new();
    let mut child = child(
        &f,
        Path::new(&f.repo.root),
        &f.sha,
        &f.temp.path().join("ready"),
    );
    assert!(child.wait().unwrap().success());
}

#[test]
fn concurrent_missing_commit_rechecks_under_common_dir_lock() {
    let f = Fixture::new();
    let linked = f.temp.path().join("linked");
    git(
        Path::new(&f.repo.root),
        &["worktree", "add", "--detach", linked.to_str().unwrap()],
    );
    assert_eq!(
        common_dir(&linked).unwrap(),
        common_dir(Path::new(&f.repo.root)).unwrap()
    );
    let sha = f.advance();
    let refs = f.refs();
    let guard = lockfile::try_lock(&f.lock()).unwrap().unwrap();
    let ready = [f.temp.path().join("ready1"), f.temp.path().join("ready2")];
    let mut a = child(&f, Path::new(&f.repo.root), &sha, &ready[0]);
    let mut b = child(&f, &linked, &sha, &ready[1]);
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while !ready.iter().all(|p| p.exists()) && std::time::Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    std::thread::sleep(Duration::from_millis(100));
    let blocked = a.try_wait().unwrap().is_none() && b.try_wait().unwrap().is_none();
    // Import while holding the same anchor: both children must recheck, not fetch.
    git(Path::new(&f.repo.root), &["fetch", "origin"]);
    drop(guard);
    let success = a.wait().unwrap().success() && b.wait().unwrap().success();
    assert!(blocked && success);
    assert_eq!(refs, f.refs());
}
