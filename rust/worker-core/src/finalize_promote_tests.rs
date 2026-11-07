use super::*;
use crate::{
    finalize::{verify_bundle, ExpectedBundle},
    writer_secret::command,
};
use sha2::{Digest, Sha256};
use std::{fs, path::Path};

const REF: &str = "refs/spawner/quarantine/promote-test";

fn git(root: &Path, args: &[&str]) -> String {
    String::from_utf8(command(root, args, &[], None).unwrap())
        .unwrap()
        .trim()
        .into()
}

struct Fixture {
    source: tempfile::TempDir,
    receiver: tempfile::TempDir,
    repo: RepoEntry,
    base: String,
    head: String,
}

impl Fixture {
    fn new() -> Self {
        let source = tempfile::tempdir().unwrap();
        let receiver = tempfile::tempdir().unwrap();
        let root = source.path();
        git(root, &["init"]);
        git(root, &["config", "user.name", "Promotion test"]);
        git(root, &["config", "user.email", "promotion@example.invalid"]);
        git(root, &["commit", "--allow-empty", "-m", "base"]);
        let base = git(root, &["rev-parse", "HEAD"]);
        let seed = root.join("seed.bundle");
        git(root, &["bundle", "create", seed.to_str().unwrap(), "HEAD"]);
        git(receiver.path(), &["init"]);
        git(
            receiver.path(),
            &["bundle", "unbundle", seed.to_str().unwrap()],
        );
        git(receiver.path(), &["update-ref", "refs/heads/base", &base]);
        fs::write(root.join("payload"), "promotion payload\n").unwrap();
        git(root, &["add", "payload"]);
        git(
            root,
            &["commit", "-m", "change\n\nPlan-Item: test\nWriter: test"],
        );
        let head = git(root, &["rev-parse", "HEAD"]);
        git(
            root,
            &[
                "bundle",
                "create",
                root.join("result.bundle").to_str().unwrap(),
                "HEAD",
            ],
        );
        let repo = RepoEntry {
            root: receiver.path().to_str().unwrap().into(),
            worktrees_root: String::new(),
            remote: String::new(),
            python: None,
            import_roots: vec![],
            cache_env: Default::default(),
        };
        Self {
            source,
            receiver,
            repo,
            base,
            head,
        }
    }
    fn verified(&self) -> VerifiedBundle {
        let path = self.source.path().join("result.bundle");
        let bytes = fs::read(&path).unwrap();
        verify_bundle(
            &self.repo,
            &path,
            &ExpectedBundle {
                sha256: &format!("{:x}", Sha256::digest(bytes)),
                base: &self.base,
                head: &self.head,
            },
        )
        .unwrap()
    }
    fn no_ref(&self) {
        assert!(command(
            self.receiver.path(),
            &["rev-parse", "--verify", REF],
            &[],
            None
        )
        .is_err());
    }
}

#[test]
fn finalize_promote_happy_and_idempotent() {
    let f = Fixture::new();
    for _ in 0..2 {
        let result = promote(&f.repo, f.verified(), REF).unwrap();
        assert_eq!(result.head, f.head);
        assert_eq!(result.quarantine_ref, REF);
        assert_eq!(git(f.receiver.path(), &["rev-parse", REF]), f.head);
        git(f.receiver.path(), &["cat-file", "-e", &f.head]);
        git(f.receiver.path(), &["fsck", "--full"]);
    }
}

#[test]
fn finalize_promote_conflicting_ref_preserves_refs() {
    let f = Fixture::new();
    let verified = f.verified();
    git(f.receiver.path(), &["update-ref", REF, &f.base]);
    let before = git(f.receiver.path(), &["for-each-ref"]);
    assert!(matches!(
        promote(&f.repo, verified, REF),
        Err(PromoteError::Ref)
    ));
    assert_eq!(git(f.receiver.path(), &["for-each-ref"]), before);
}

#[test]
fn finalize_promote_readonly_pack_has_no_ref() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let verified = f.verified();
    let dir = f.receiver.path().join(".git/objects/pack");
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o555)).unwrap();
    // Root bypasses directory permissions; also exercise a deterministic missing source.
    if unsafe { libc::geteuid() } == 0 {
        fs::remove_file(&verified.pack).unwrap();
    }
    let result = promote(&f.repo, verified, REF);
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(matches!(result, Err(PromoteError::Install)));
    f.no_ref();
}

#[test]
fn finalize_promote_ref_lock_failure_is_retryable() {
    let f = Fixture::new();
    let verified = f.verified();
    let lock = f.receiver.path().join(format!(".git/{REF}.lock"));
    fs::create_dir_all(lock.parent().unwrap()).unwrap();
    fs::write(&lock, "locked").unwrap();
    assert!(matches!(
        promote(&f.repo, verified, REF),
        Err(PromoteError::Ref)
    ));
    f.no_ref();
    git(f.receiver.path(), &["cat-file", "-e", &f.head]);
    fs::remove_file(lock).unwrap();
    promote(&f.repo, f.verified(), REF).unwrap();
}

#[test]
fn finalize_promote_concurrent_identical_import() {
    let f = Fixture::new();
    let a = f.verified();
    let b = f.verified();
    std::thread::scope(|s| {
        let first = s.spawn(|| promote(&f.repo, a, REF));
        let second = s.spawn(|| promote(&f.repo, b, REF));
        first.join().unwrap().unwrap();
        second.join().unwrap().unwrap();
    });
    assert_eq!(git(f.receiver.path(), &["rev-parse", REF]), f.head);
}

#[test]
fn finalize_promote_collision_and_invalid_ref() {
    let f = Fixture::new();
    let verified = f.verified();
    let target = f
        .receiver
        .path()
        .join(".git/objects/pack")
        .join(verified.pack.file_name().unwrap());
    fs::write(target, "different content").unwrap();
    assert!(matches!(
        promote(&f.repo, verified, REF),
        Err(PromoteError::Install)
    ));
    f.no_ref();
    assert!(matches!(
        promote(&f.repo, f.verified(), "refs/heads/invalid"),
        Err(PromoteError::Invalid)
    ));
}
