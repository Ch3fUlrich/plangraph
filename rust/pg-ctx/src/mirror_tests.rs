use super::*;
use std::sync::atomic::{AtomicU64, Ordering};

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
        let message = format!("fixture {}", COMMIT.fetch_add(1, Ordering::Relaxed));
        let mut args = vec!["commit-tree", &tree, "-m", &message];
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
    assert_eq!(f.mirror.watermark("demo").unwrap(), Some(second.clone()));
    assert_eq!(
        serde_json::from_slice::<Watermark>(&f.bytes()).unwrap(),
        second
    );
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
    assert_eq!(f.mirror.fetch("demo").unwrap().main_head, c);
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
        fetched_at: 1000,
    };
    assert_eq!(w.age_at(999), 0);
    assert_eq!(w.age_at(1299), 299);
    assert!(!w.is_stale_at(1299));
    assert!(w.is_stale_at(1300));
    assert!(w.is_stale_at(1301));
}
