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
        Self::with_format(mode, "sha1")
    }
    fn with_format(mode: &str, format: &str) -> Self {
        let source = tempfile::tempdir().unwrap();
        let root = source.path();
        git(root, &["init", &format!("--object-format={format}")]);
        git(root, &["config", "user.name", "Finalize test"]);
        git(root, &["config", "user.email", "finalize@example.invalid"]);
        git(root, &["commit", "--allow-empty", "-m", "base"]);
        let base = git(root, &["rev-parse", "HEAD"]);
        let receiver = tempfile::tempdir().unwrap();
        git(
            receiver.path(),
            &["init", &format!("--object-format={format}")],
        );
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
        if mode == "blob" || mode == "message" || mode == "multi" {
            // The receiver must detect secrets even when absent from the final tip.
            if mode == "blob" {
                git(root, &["rm", "payload"]);
            }
            git(
                root,
                &["commit", "--allow-empty", "-m", "clean\n\nWriter: test"],
            );
        }
        if mode == "empty" {
            git(root, &["reset", "--soft", &base]);
        }
        if mode == "tag" {
            git(root, &["tag", "-a", "result", "-m", "annotated"]);
        }
        let tip = if mode == "tag" {
            "refs/tags/result"
        } else {
            "HEAD"
        };
        let head = git(root, &["rev-parse", tip]);
        let bundle = root.join("result.bundle");
        let mut args = vec!["bundle", "create", bundle.to_str().unwrap(), tip];
        if mode == "refs" {
            git(root, &["branch", "extra"]);
            args.push("refs/heads/extra");
        }
        git(root, &args);
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
    fn assert_refusal(&self, expected: Refusal) {
        let root = Path::new(&self.repo.root);
        let refs = || git(root, &["for-each-ref", "--format=%(refname) %(objectname)"]);
        let before = refs();
        let objects = all_objects(root);
        let reachable = git(root, &["rev-list", "--objects", "--all"]);
        assert_eq!(self.import(), Err(expected));
        assert_eq!(refs(), before);
        assert_eq!(all_objects(root), objects);
        assert_eq!(git(root, &["rev-list", "--objects", "--all"]), reachable);
        assert!(git(root, &["for-each-ref", "refs/spawner/quarantine/test"]).is_empty());
        assert!(!root.join(".git/FETCH_HEAD").exists());
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
    assert_eq!(
        all_objects(Path::new(&f.repo.root)),
        git(
            f._source.path(),
            &["rev-list", "--objects", "--no-object-names", &f.head]
        )
        .lines()
        .map(str::to_owned)
        .collect()
    );
}

fn all_objects(root: &Path) -> std::collections::BTreeSet<String> {
    git(
        root,
        &[
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        ],
    )
    .lines()
    .map(str::to_owned)
    .collect()
}

#[test]
fn refuses_unreachable_pack_blob() {
    let mut f = Fixture::new("safe");
    let root = f._source.path();
    let blob = command(
        root,
        &["hash-object", "-w", "--stdin"],
        b"unreachable payload",
        None,
    )
    .unwrap();
    let mut objects = command(
        root,
        &["rev-list", "--objects", "--no-object-names", &f.head],
        &[],
        None,
    )
    .unwrap();
    objects.extend(blob);
    let pack = command(root, &["pack-objects", "--stdout"], &objects, None).unwrap();
    f.bytes = format!("# v2 git bundle\n{} HEAD\n\n", f.head).into_bytes();
    f.bytes.extend(pack);
    f.assert_refusal(Refusal::UnexpectedObjects);
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
fn refuses_loose_extra_object() {
    let f = Fixture::new("safe");
    let root = f._source.path();
    assert_eq!(validate_objects(root, &f.head, &f.base), Ok(()));
    command(
        root,
        &["hash-object", "-w", "--stdin"],
        b"loose extra",
        None,
    )
    .unwrap();
    assert_eq!(
        validate_objects(root, &f.head, &f.base),
        Err(Refusal::UnexpectedObjects)
    );
}

#[test]
fn refused_create_only_ref_rolls_back_pack() {
    let f = Fixture::new("safe");
    let root = Path::new(&f.repo.root);
    let reference = "refs/spawner/quarantine/test";
    git(root, &["update-ref", reference, &f.base]);
    let before = all_objects(root);
    assert_eq!(f.import(), Err(Refusal::BadBundle));
    assert_eq!(all_objects(root), before);
    assert_eq!(git(root, &["rev-parse", reference]), f.base);
}

#[test]
fn sha256_objects() {
    let probe = tempfile::tempdir().unwrap();
    if command(probe.path(), &["init", "--object-format=sha256"], &[], None).is_err() {
        eprintln!("SKIP sha256_objects: host Git lacks SHA-256 init support");
        return;
    }
    let f = Fixture::with_format("safe", "sha256");
    assert!(f.import().is_ok());
    let root = f._source.path();
    command(
        root,
        &["hash-object", "-w", "--stdin"],
        b"sha256 extra",
        None,
    )
    .unwrap();
    assert_eq!(
        validate_objects(root, &f.head, &f.base),
        Err(Refusal::UnexpectedObjects)
    );
}
#[test]
fn refuses_bad_bundle() {
    let mut f = Fixture::new("safe");
    f.bytes = b"not a bundle".to_vec();
    f.assert_refusal(Refusal::BadBundle);
}
#[test]
fn refuses_not_descendant() {
    let f = Fixture::new("unrelated");
    f.assert_refusal(Refusal::NotDescendant);
}
#[test]
fn refuses_missing_trailer() {
    let f = Fixture::new("trailer");
    f.assert_refusal(Refusal::MissingTrailer);
}
#[test]
fn refuses_secret_in_history_blobs_and_messages() {
    for mode in ["blob", "message"] {
        let f = Fixture::new(mode);
        f.assert_refusal(Refusal::SecretInHistory);
    }
}
#[test]
fn refuses_annotated_tag_head() {
    Fixture::new("tag").assert_refusal(Refusal::BadBundle);
}
#[test]
fn refuses_multi_ref_bundle() {
    Fixture::new("refs").assert_refusal(Refusal::BadBundle);
}
#[test]
fn refuses_empty_range() {
    Fixture::new("empty").assert_refusal(Refusal::BadBundle);
}
#[test]
fn passing_multi_commit_import() {
    let f = Fixture::new("multi");
    let result = f.import().unwrap();
    assert_eq!(result.head_sha, f.head);
    assert_eq!(
        git(
            Path::new(&f.repo.root),
            &[
                "rev-list",
                "--count",
                &format!("{}..{}", f.base, result.reference)
            ]
        ),
        "2"
    );
}
