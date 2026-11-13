use super::*;
use pg_worker_core::workspace_resolver::git_command;
use std::sync::atomic::{AtomicU64, Ordering};

#[path = "mirror_safety_tests.rs"]
mod safety;
#[path = "mirror_transport_tests.rs"]
mod transport;

static COMMIT: AtomicU64 = AtomicU64::new(0);

fn git(dir: &Path, args: &[&str]) -> String {
    let out = git_command(dir)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", dir.join("absent-config"))
        .env("GIT_AUTHOR_NAME", "Mirror Test")
        .env("GIT_COMMITTER_NAME", "Mirror Test")
        .env("GIT_AUTHOR_EMAIL", "mirror@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "mirror@example.invalid")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}

struct Fixture {
    _tmp: tempfile::TempDir,
    origin: PathBuf,
    mirror: Mirror,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let origin = tmp.path().join("origin.git");
        fs::create_dir(&origin).unwrap();
        git(&origin, &["init", "--bare", "--initial-branch=main"]);
        let config = tmp.path().join("repos.json");
        fs::write(
            &config,
            serde_json::to_vec(&serde_json::json!({"demo": origin})).unwrap(),
        )
        .unwrap();
        let mirror = Mirror::from_repos(&config, &tmp.path().join("mirrors")).unwrap();
        Self {
            _tmp: tmp,
            origin,
            mirror,
        }
    }
    fn commit(&self, parent: Option<&str>) -> String {
        let tree = git(&self.origin, &["mktree"]);
        self.commit_tree(parent, &tree)
    }
    fn commit_tree(&self, parent: Option<&str>, tree: &str) -> String {
        let message = format!("fixture {}", COMMIT.fetch_add(1, Ordering::Relaxed));
        let mut args = vec!["commit-tree", tree, "-m", &message];
        if let Some(parent) = parent {
            args.extend(["-p", parent]);
        }
        let head = git(&self.origin, &args);
        git(&self.origin, &["update-ref", "refs/heads/main", &head]);
        head
    }
    fn bytes(&self) -> Vec<u8> {
        fs::read(self.mirror.root.join("demo/watermark.json")).unwrap()
    }
    fn assert_persisted(&self, watermark: &Watermark) {
        assert_eq!(
            self.mirror.watermark("demo").unwrap().as_ref(),
            Some(watermark)
        );
        assert_eq!(
            &serde_json::from_slice::<Watermark>(&self.bytes()).unwrap(),
            watermark
        );
    }
}

#[test]
fn mirror_first_fetch_and_advance_are_bare_and_persisted() {
    let f = Fixture::new();
    let a = f.commit(None);
    let first = f.mirror.fetch("demo").unwrap();
    assert_eq!(
        (first.repo.as_str(), first.main_head.as_str()),
        ("demo", a.as_str())
    );
    assert!(first.fetched_at > 0);
    assert!(first.age() < 300);
    assert!(!first.is_stale());
    let dir = f.mirror.mirror_dir("demo").unwrap();
    assert_eq!(git(&dir, &["rev-parse", "--is-bare-repository"]), "true");
    assert!(!dir.join(".git").exists());
    assert!(!dir.join("worktrees").exists());
    let b = f.commit(Some(&a));
    let second = f.mirror.fetch("demo").unwrap();
    assert_eq!(second.main_head, b);
    assert!(second.fetched_at >= first.fetched_at);
    f.assert_persisted(&second);
    assert_eq!(git(&dir, &["rev-parse", "main"]), b);
}

#[test]
fn mirror_rewind_and_divergence_never_replace_the_watermark() {
    let f = Fixture::new();
    let a = f.commit(None);
    let b = f.commit(Some(&a));
    let old = f.mirror.fetch_at("demo", 200).unwrap();
    let bytes = f.bytes();
    git(&f.origin, &["update-ref", "refs/heads/main", &a]);
    for _ in 0..2 {
        assert_eq!(f.mirror.fetch_at("demo", 300), Err(Alarm::ForcePush));
        assert_eq!(f.bytes(), bytes);
    }
    let divergent = f.commit(Some(&a));
    assert_ne!(divergent, b);
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
    assert_eq!(f.mirror.watermark("demo").unwrap(), Some(old));
    let dir = f.mirror.mirror_dir("demo").unwrap();
    assert_eq!(git(&dir, &["rev-parse", "main"]), b);
    assert!(git(
        &dir,
        &["for-each-ref", "--format=%(objectname)", "refs/pg-ctx"]
    )
    .is_empty());
    assert!(
        !git_command(&dir)
            .args(["cat-file", "-e", &divergent])
            .output()
            .unwrap()
            .status
            .success(),
        "rejected objects must remain outside the accepted mirror"
    );
    git(&f.origin, &["update-ref", "refs/heads/main", &b]);
    let c = f.commit(Some(&b));
    assert_eq!(f.mirror.fetch_at("demo", 100).unwrap().main_head, c);
    assert_eq!(f.mirror.watermark("demo").unwrap().unwrap().fetched_at, 200);
}

#[test]
fn mirror_without_watermark_refuses_rewind_and_divergence() {
    let f = Fixture::new();
    let a = f.commit(None);
    let b = f.commit(Some(&a));
    f.mirror.fetch("demo").unwrap();
    let dir = f.mirror.mirror_dir("demo").unwrap();
    fs::remove_file(dir.with_file_name("watermark.json")).unwrap();
    git(&f.origin, &["update-ref", "refs/heads/main", &a]);
    for _ in 0..2 {
        assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
        assert_eq!(git(&dir, &["rev-parse", "main"]), b);
        assert_eq!(f.mirror.watermark("demo").unwrap(), None);
    }
    f.commit(Some(&a));
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
    assert_eq!(git(&dir, &["rev-parse", "main"]), b);
    assert_eq!(f.mirror.watermark("demo").unwrap(), None);
    let c = f.commit(Some(&b));
    let recovered = f.mirror.fetch("demo").unwrap();
    assert_eq!(recovered.main_head, c);
    f.assert_persisted(&recovered);
}

#[test]
fn mirror_without_watermark_or_main_ref_refuses_fetch() {
    let f = Fixture::new();
    f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let dir = f.mirror.mirror_dir("demo").unwrap();
    fs::remove_file(dir.with_file_name("watermark.json")).unwrap();
    git(&dir, &["update-ref", "-d", "refs/heads/main"]);
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert_eq!(f.mirror.watermark("demo").unwrap(), None);
}

#[test]
fn mirror_rebuilt_without_baseline_refuses_rewind_until_history_returns() {
    let f = Fixture::new();
    let a = f.commit(None);
    let b = f.commit(Some(&a));
    f.mirror.fetch("demo").unwrap();
    let bytes = f.bytes();
    let dir = f.mirror.mirror_dir("demo").unwrap();
    fs::rename(&dir, dir.with_file_name("saved.git")).unwrap();
    git(&f.origin, &["update-ref", "refs/heads/main", &a]);
    // Local clones otherwise copy unreachable objects too; really lose the baseline.
    git(&f.origin, &["reflog", "expire", "--expire=now", "--all"]);
    git(&f.origin, &["gc", "--prune=now"]);
    for _ in 0..2 {
        assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
        assert_eq!(f.bytes(), bytes);
        assert!(!dir.exists(), "never publish a rejected rebuilt mirror");
    }
    let saved = dir.with_file_name("saved.git");
    git(&f.origin, &["fetch", saved.to_str().unwrap(), "main"]);
    let c = f.commit(Some(&b));
    let recovered = f.mirror.fetch("demo").unwrap();
    assert_eq!(recovered.main_head, c);
    f.assert_persisted(&recovered);
}

#[test]
fn mirror_rebuilt_with_fast_forward_history_recovers_without_deleting_watermark() {
    let f = Fixture::new();
    let source = open(&f.origin).unwrap();
    let blob = source
        .write_blob(b"retained named file\n")
        .unwrap()
        .detach();
    git(
        &f.origin,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            "100644",
            &blob.to_string(),
            "README.md",
        ],
    );
    let tree = git(&f.origin, &["write-tree"]);
    let a = f.commit_tree(None, &tree);
    let first = f
        .mirror
        .fetch_named_at("demo", Some(&["README.md"]), 200)
        .unwrap();
    assert_eq!(first.blob_shas["README.md"], blob.to_string());
    let bytes = f.bytes();
    let dir = f.mirror.mirror_dir("demo").unwrap();
    fs::rename(&dir, dir.with_file_name("saved.git")).unwrap();
    assert_eq!(f.bytes(), bytes);
    let b = f.commit_tree(Some(&a), &tree);
    let recovered = f.mirror.fetch_at("demo", 100).unwrap();
    assert_eq!(recovered.main_head, b);
    assert_eq!(recovered.repo, first.repo);
    assert_eq!(recovered.blob_shas, first.blob_shas);
    assert_eq!(recovered.fetched_at, first.fetched_at);
    f.assert_persisted(&recovered);
}

#[test]
fn mirror_existing_without_baseline_refuses_and_then_recovers_history() {
    let f = Fixture::new();
    let a = f.commit(None);
    let b = f.commit(Some(&a));
    f.mirror.fetch("demo").unwrap();
    let bytes = f.bytes();
    let dir = f.mirror.mirror_dir("demo").unwrap();
    let saved = dir.with_file_name("saved.git");
    fs::rename(&dir, &saved).unwrap();
    fs::create_dir(&dir).unwrap();
    git(&dir, &["init", "--bare", "--initial-branch=main"]);
    git(&f.origin, &["update-ref", "refs/heads/main", &a]);
    git(&f.origin, &["reflog", "expire", "--expire=now", "--all"]);
    git(&f.origin, &["gc", "--prune=now"]);
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
    assert_eq!(f.bytes(), bytes);
    git(&f.origin, &["fetch", saved.to_str().unwrap(), "main"]);
    let c = f.commit(Some(&b));
    let recovered = f.mirror.fetch("demo").unwrap();
    assert_eq!(recovered.main_head, c);
    f.assert_persisted(&recovered);
}

#[test]
fn mirror_unreachable_and_deleted_main_keep_old_bytes() {
    let f = Fixture::new();
    f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let old = f.bytes();
    let offline = f.origin.with_extension("offline");
    fs::rename(&f.origin, &offline).unwrap();
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert_eq!(f.bytes(), old);
    fs::rename(offline, &f.origin).unwrap();
    git(&f.origin, &["update-ref", "-d", "refs/heads/main"]);
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert_eq!(f.bytes(), old);
}

#[test]
fn mirror_config_validation_and_failed_clone_retry() {
    let f = Fixture::new();
    assert_eq!(f.mirror.watermark("demo").unwrap(), None);
    assert_eq!(f.mirror.fetch("unknown"), Err(Alarm::FetchFailed));
    let offline = f.origin.with_extension("offline");
    fs::rename(&f.origin, &offline).unwrap();
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert!(!f.mirror.mirror_dir("demo").unwrap().exists());
    fs::rename(offline, &f.origin).unwrap();
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert!(!f.mirror.mirror_dir("demo").unwrap().exists());
    assert_eq!(f.mirror.watermark("demo").unwrap(), None);
    f.commit(None);
    assert!(f.mirror.fetch("demo").is_ok());
    let config = f._tmp.path().join("repos.json");
    for bad in [r#"{"../escape":"local"}"#, r#"{"demo":""}"#, "[]", "{"] {
        fs::write(&config, bad).unwrap();
        assert!(Mirror::from_repos(&config, &f.mirror.root).is_err());
    }
}

#[test]
fn mirror_age_stale_boundary_and_clock_rollback() {
    let w = Watermark {
        repo: "demo".into(),
        main_head: "abc".into(),
        blob_shas: BTreeMap::new(),
        fetched_at: 1000,
    };
    assert_eq!(w.age_at(999), 0);
    assert_eq!(w.age_at(1299), 299);
    assert!(!w.is_stale_at(1299));
    assert!(!w.is_stale_at(1300));
    assert!(w.is_stale_at(1301));
}

#[test]
fn mirror_overlapping_fetch_waits_for_the_writer() {
    use std::io::{BufRead, BufReader, Write};
    use std::process::{Command, Stdio};
    let f = Fixture::new();
    let head = f.commit(None);
    let dir = f.mirror.root.join("demo");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("fetch.lock");
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "mirror::tests::safety::mirror_lock_holder_child",
            "--nocapture",
        ])
        .env("PG_CTX_LOCK_FIXTURE", &path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    loop {
        let mut line = String::new();
        assert!(
            output.read_line(&mut line).unwrap() > 0,
            "lock child exited before acquiring lock"
        );
        if line.trim() == "locked" {
            break;
        }
    }
    // Drive the real fetch path to its deadline without wall-clock sleeps.
    let elapsed = std::cell::Cell::new(Duration::ZERO);
    assert_eq!(
        f.mirror.fetch_named_with_wait(
            "demo",
            None,
            now(),
            || elapsed.get(),
            |delay| elapsed.set(elapsed.get() + delay),
        ),
        Err(Alarm::FetchFailed),
    );
    assert_eq!(elapsed.get(), FETCH_LOCK_TIMEOUT);
    assert_eq!(f.mirror.watermark("demo").unwrap(), None);
    elapsed.set(Duration::ZERO);
    let mut contended = false;
    let watermark = f
        .mirror
        .fetch_named_with_wait(
            "demo",
            None,
            now(),
            || elapsed.get(),
            |delay| {
                elapsed.set(elapsed.get() + delay);
                assert!(!contended, "writer must release before retry");
                contended = true;
                child
                    .stdin
                    .as_mut()
                    .unwrap()
                    .write_all(b"release\n")
                    .unwrap();
                assert!(child.wait().unwrap().success());
            },
        )
        .unwrap();
    // Always reap the helper, including an unexpectedly non-contending lock impl.
    child.kill().ok();
    assert!(child.wait().unwrap().success());
    assert!(contended, "another process must hold the writer lock");
    assert!(elapsed.get() > Duration::ZERO && elapsed.get() < FETCH_LOCK_TIMEOUT);
    assert_eq!(watermark.main_head, head);
}
