use super::*;
use crate::{
    finalize::{verify_bundle, ExpectedBundle},
    writer_secret::command,
};
use sha2::{Digest, Sha256};
use std::{fs, path::Path};

const REF: &str = "refs/spawner/quarantine/promote-test";

fn git(root: &Path, args: &[&str]) -> String {
    // command isolates global/system config; pin local signing, hooks and templates too.
    let mut isolated = vec![
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "init.templateDir=",
    ];
    isolated.extend_from_slice(args);
    String::from_utf8(command(root, &isolated, &[], None).unwrap())
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
        git(root, &["init", "--initial-branch=promotion-test"]);
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
                "refs/heads/promotion-test",
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
        // RESULT verification accepts one named branch tip, not the HEAD pseudo-ref.
        assert_eq!(
            git(
                self.source.path(),
                &["bundle", "list-heads", path.to_str().unwrap()]
            ),
            format!("{} refs/heads/promotion-test", self.head)
        );
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
    match promote(&f.repo, verified, REF).unwrap_err() {
        PromoteError::Ref => {}
        PromoteError::Cause { kind, .. } if matches!(*kind, PromoteError::Ref) => {}
        error => panic!("expected ref contention, got {error:?}"),
    }
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
        Err(PromoteError::Cause { kind, .. }) if matches!(*kind, PromoteError::Ref)
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

#[test]
fn finalize_promote_setup_retains_install_cause() {
    let mut f = Fixture::new();
    let verified = f.verified();
    f.repo.root = f.source.path().join("absent").to_str().unwrap().into();
    let error = promote(&f.repo, verified, REF).unwrap_err();
    assert!(
        matches!(&error, PromoteError::Cause { kind, .. } if matches!(**kind, PromoteError::Install))
    );
    assert!(std::error::Error::source(&error).is_some());
}

#[test]
fn finalize_promote_rejects_wrong_pack_name() {
    let f = Fixture::new();
    let mut verified = f.verified();
    let wrong = verified
        .pack
        .with_file_name(format!("pack-{}.pack", "0".repeat(f.head.len())));
    fs::rename(&verified.pack, &wrong).unwrap();
    fs::rename(&verified.idx, wrong.with_extension("idx")).unwrap();
    verified.pack = wrong;
    verified.idx = verified.pack.with_extension("idx");
    assert!(matches!(
        promote(&f.repo, verified, REF),
        Err(PromoteError::Invalid)
    ));
    f.no_ref();
}

#[test]
fn finalize_promote_exdev_mode_collision_and_failure() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let target = dir.path().join("target");
    fs::write(&source, "pack content").unwrap();
    for _ in 0..2 {
        let mut calls = 0;
        super::unix::install_with(&source, &target, |a, b| {
            calls += 1;
            if calls == 1 {
                Err(std::io::Error::from_raw_os_error(libc::EXDEV))
            } else {
                fs::hard_link(a, b)
            }
        })
        .unwrap();
        assert_eq!(calls, 2);
        assert_eq!(
            fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o444
        );
        assert_eq!(fs::read(&target).unwrap(), fs::read(&source).unwrap());
    }
    let mut calls = 0;
    assert!(super::unix::install_with(&source, &target, |_, _| {
        calls += 1;
        Err(std::io::Error::from_raw_os_error(if calls == 1 {
            libc::EXDEV
        } else {
            libc::EACCES
        }))
    })
    .is_err());
}

#[test]
fn finalize_promote_cleanup_preserves_objects_error_and_retries() {
    let f = Fixture::new();
    let mut attempts = 0;
    super::unix::cleanup(|| {
        attempts += 1;
        if attempts == 1 {
            Err(PromoteError::Ref)
        } else {
            Ok(())
        }
    });
    assert_eq!(attempts, 2);
    let mut resolves = 0;
    let result = super::unix::promote_with(&f.repo, f.verified(), REF, |_, _| {
        resolves += 1;
        if resolves == 1 {
            return Ok(());
        }
        let lock = f.receiver.path().join(format!(".git/{REF}.lock"));
        fs::write(lock, "locked").unwrap();
        Err(PromoteError::Objects)
    });
    assert!(matches!(result, Err(PromoteError::Objects)));
    fs::remove_file(f.receiver.path().join(format!(".git/{REF}.lock"))).unwrap();
    let mut resolves = 0;
    let result = super::unix::promote_with(
        &f.repo,
        f.verified(),
        "refs/spawner/quarantine/replacement",
        |_, _| {
            resolves += 1;
            if resolves == 1 {
                return Ok(());
            }
            git(
                f.receiver.path(),
                &["update-ref", "refs/spawner/quarantine/replacement", &f.base],
            );
            Err(PromoteError::Objects)
        },
    );
    assert!(matches!(result, Err(PromoteError::Objects)));
    assert_eq!(
        git(
            f.receiver.path(),
            &["rev-parse", "refs/spawner/quarantine/replacement"]
        ),
        f.base
    );
}

#[test]
fn finalize_promote_fixture_ignores_local_signing_and_hooks() {
    let f = Fixture::new();
    git(f.source.path(), &["config", "commit.gpgsign", "true"]);
    git(
        f.source.path(),
        &["config", "core.hooksPath", "/nonexistent/hooks"],
    );
    git(
        f.source.path(),
        &["commit", "--allow-empty", "-m", "isolated"],
    );
}

#[test]
fn finalize_promote_batches_all_objects_and_reports_missing() {
    let f = Fixture::new();
    let verified = f.verified();
    super::unix::BATCH_INPUTS.with(|inputs| inputs.borrow_mut().clear());
    super::unix::resolve(f.source.path(), &verified).unwrap();
    super::unix::BATCH_INPUTS.with(|inputs| {
        let inputs = inputs.borrow();
        assert_eq!(inputs.len(), 1);
        assert!(
            inputs[0]
                .split(|b| *b == b'\n')
                .filter(|s| !s.is_empty())
                .count()
                >= 4
        );
    });
    // The appended head duplicates rev-list output. Missing requires repository mutation
    // between calls, so exercise the successful-exit missing response synthetically.
    let input = format!("{}\n", f.head);
    let output = format!("{} missing\n", f.head);
    assert!(matches!(
        super::unix::validate_batch(input.as_bytes(), output.as_bytes()),
        Err(PromoteError::Objects)
    ));
}

#[test]
fn finalize_promote_non_exdev_link_failure_has_no_ref() {
    let f = Fixture::new();
    let verified = f.verified();
    let target = f.receiver.path().join(".git/objects/pack/failed.pack");
    assert!(matches!(
        super::unix::install_with(&verified.pack, &target, |_, _| {
            Err(std::io::Error::from_raw_os_error(libc::EACCES))
        }),
        Err(PromoteError::Install)
    ));
    assert!(!target.exists());
    assert!(fs::read_dir(target.parent().unwrap())
        .unwrap()
        .all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with("failed.pack.")));
    f.no_ref();
}
