use super::*;
use tempfile::TempDir;

fn git(root: &Path, args: &[&str]) -> String {
    String::from_utf8(command(root, args, &[], None).unwrap())
        .unwrap()
        .trim()
        .into()
}

struct Fixture {
    _source: TempDir,
    _receiver: TempDir,
    repo: RepoEntry,
    base: String,
    head: String,
    bytes: Vec<u8>,
}
impl Fixture {
    fn new(mode: &str) -> Self {
        let source = tempfile::tempdir().unwrap();
        let root = source.path();
        git(root, &["init"]);
        git(root, &["commit", "--allow-empty", "-m", "base"]);
        let base = git(root, &["rev-parse", "HEAD"]);
        let receiver = tempfile::tempdir().unwrap();
        git(receiver.path(), &["init"]);
        // Seed prerequisites via a local bundle, never a network or user repository.
        let seed = root.join("seed.bundle");
        git(root, &["bundle", "create", seed.to_str().unwrap(), "HEAD"]);
        git(
            receiver.path(),
            &["bundle", "unbundle", seed.to_str().unwrap()],
        );
        git(
            receiver.path(),
            &["update-ref", "refs/heads/lane/test", &base],
        );
        if mode == "unrelated" {
            git(root, &["checkout", "--orphan", "other"]);
        }
        let token = ["gh", "p_", &"a".repeat(25)].concat();
        let message = if mode == "message" {
            format!("change {token}\n\nWriter: test")
        } else if mode == "trailer" {
            "change".into()
        } else {
            "change\n\nWriter: test".into()
        };
        if mode == "blob" {
            std::fs::write(root.join("payload"), token).unwrap();
            git(root, &["add", "payload"]);
        }
        git(root, &["commit", "--allow-empty", "-m", &message]);
        if mode == "blob" || mode == "message" {
            // The receiver must detect secrets even when absent from the final tip.
            if mode == "blob" {
                git(root, &["rm", "payload"]);
            }
            git(
                root,
                &["commit", "--allow-empty", "-m", "clean\n\nWriter: test"],
            );
        }
        let head = git(root, &["rev-parse", "HEAD"]);
        let bundle = root.join("result.bundle");
        git(
            root,
            &["bundle", "create", bundle.to_str().unwrap(), "HEAD"],
        );
        let bytes = std::fs::read(bundle).unwrap();
        let repo = RepoEntry {
            root: receiver.path().to_str().unwrap().into(),
            worktrees_root: String::new(),
            remote: String::new(),
            python: None,
            import_roots: vec![],
            cache_env: Default::default(),
        };
        Self {
            _source: source,
            _receiver: receiver,
            repo,
            base,
            head,
            bytes,
        }
    }
    fn import(&self) -> Result<QuarantinedResult, Refusal> {
        import_result(
            &self.repo,
            &self.bytes,
            &format!("{:x}", Sha256::digest(&self.bytes)),
            &self.base,
            &self.head,
            "test",
        )
    }
    fn public_refs(&self) -> String {
        git(
            Path::new(&self.repo.root),
            &["for-each-ref", "--format=%(refname) %(objectname)"],
        )
        .lines()
        .filter(|s| !s.starts_with("refs/spawner/quarantine/"))
        .collect::<Vec<_>>()
        .join("\n")
    }
}

#[test]
fn passing_quarantine_import() {
    let f = Fixture::new("safe");
    let before = f.public_refs();
    let result = f.import().unwrap();
    assert_eq!(result.head_sha, f.head);
    assert_eq!(
        git(Path::new(&f.repo.root), &["rev-parse", &result.reference]),
        f.head
    );
    assert_eq!(f.public_refs(), before);
}
#[test]
fn refuses_hash_mismatch() {
    let f = Fixture::new("safe");
    assert_eq!(
        import_result(&f.repo, &f.bytes, "wrong", &f.base, &f.head, "test"),
        Err(Refusal::HashMismatch)
    );
}
#[test]
fn refuses_bad_bundle() {
    let mut f = Fixture::new("safe");
    f.bytes = b"not a bundle".to_vec();
    assert_eq!(f.import(), Err(Refusal::BadBundle));
}
#[test]
fn refuses_not_descendant() {
    let f = Fixture::new("unrelated");
    assert_eq!(f.import(), Err(Refusal::NotDescendant));
}
#[test]
fn refuses_missing_trailer() {
    let f = Fixture::new("trailer");
    assert_eq!(f.import(), Err(Refusal::MissingTrailer));
}
#[test]
fn refuses_secret_in_history_blobs_and_messages() {
    for mode in ["blob", "message"] {
        let f = Fixture::new(mode);
        assert_eq!(f.import(), Err(Refusal::SecretInHistory));
    }
}
#[test]
fn refusals_never_change_refs_outside_quarantine() {
    for mode in ["safe", "unrelated", "trailer", "blob", "message", "bad"] {
        let mut f = Fixture::new(mode);
        let before = f.public_refs();
        if mode == "safe" {
            f.bytes.push(0);
        }
        if mode == "bad" {
            f.bytes.clear();
        }
        let result = if mode == "safe" {
            import_result(&f.repo, &f.bytes, "wrong", &f.base, &f.head, "test")
        } else {
            f.import()
        };
        assert!(result.is_err());
        assert_eq!(f.public_refs(), before);
        assert!(!Path::new(&f.repo.root).join(".git/FETCH_HEAD").exists());
    }
}
