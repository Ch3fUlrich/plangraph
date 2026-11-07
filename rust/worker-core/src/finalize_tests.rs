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
            format!("change {token}\n\nPlan-Item: test\nWriter: test")
        } else if mode == "plan" {
            "change\n\nWriter: test".into()
        } else if mode == "writer" {
            "change\n\nPlan-Item: test".into()
        } else if mode == "trailer" {
            "change".into()
        } else {
            "change\n\nPlan-Item: test\nWriter: test".into()
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
                &[
                    "commit",
                    "--allow-empty",
                    "-m",
                    "clean\n\nPlan-Item: test\nWriter: test",
                ],
            );
        }
        if mode == "empty" {
            git(root, &["reset", "--soft", &base]);
        }
        if mode == "lightweight" {
            git(root, &["tag", "result"]);
        }
        if mode == "tag" {
            git(root, &["tag", "-a", "result", "-m", "annotated"]);
        }
        let tip = if mode == "tag" || mode == "lightweight" {
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
    fn import(&self) -> Result<VerifiedBundle, Refusal> {
        let root = Path::new(&self.repo.root);
        let before = snapshot(root);
        let file = self._source.path().join("verify.bundle");
        std::fs::write(&file, &self.bytes).unwrap();
        let result = verify_bundle(
            &self.repo,
            &file,
            &ExpectedBundle {
                sha256: &format!("{:x}", Sha256::digest(&self.bytes)),
                base: &self.base,
                head: &self.head,
            },
        );
        assert_eq!(snapshot(root), before);
        result
    }
    fn assert_refusal(&self, expected: Refusal) {
        assert_eq!(self.import().unwrap_err(), expected);
    }
}

fn snapshot(root: &Path) -> (Vec<u8>, Vec<u8>) {
    (
        command(root, &["for-each-ref"], &[], None).unwrap(),
        command(
            root,
            &["cat-file", "--batch-all-objects", "--batch-check"],
            &[],
            None,
        )
        .unwrap(),
    )
}

#[test]
fn passing_verified_bundle() {
    for format in ["sha1", "sha256"] {
        let f = Fixture::with_format("safe", format);
        let result = f.import().unwrap();
        assert_eq!(result.head, f.head);
        assert_eq!(result.base, f.base);
        assert!(result.pack.is_file());
        assert!(result.idx.is_file());
        assert!(!result.pack.starts_with(&f.repo.root));
        let path = result.pack.clone();
        drop(result);
        assert!(!path.exists());
    }
}

#[test]
fn unrelated_receiver_objects_are_not_bundle_content() {
    let f = Fixture::new("safe");
    command(
        Path::new(&f.repo.root),
        &["hash-object", "-w", "--stdin"],
        b"other run",
        None,
    )
    .unwrap();
    f.import().unwrap();
}

#[test]
fn refuses_unreachable_pack_blob() {
    for format in ["sha1", "sha256"] {
        let mut f = Fixture::with_format("safe", format);
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
        f.bytes = if format == "sha256" {
            format!(
                "# v3 git bundle\n@object-format=sha256\n{} HEAD\n\n",
                f.head
            )
        } else {
            format!("# v2 git bundle\n{} HEAD\n\n", f.head)
        }
        .into_bytes();
        f.bytes.extend(pack);
        f.assert_refusal(Refusal::UnexpectedObjects);
    }
}
#[test]
fn refuses_hash_mismatch() {
    let f = Fixture::new("safe");
    let root = Path::new(&f.repo.root);
    let before = snapshot(root);
    let path = f._source.path().join("bad.bundle");
    std::fs::write(&path, &f.bytes).unwrap();
    assert_eq!(
        verify_bundle(
            &f.repo,
            &path,
            &ExpectedBundle {
                sha256: "wrong",
                base: &f.base,
                head: &f.head,
            }
        )
        .unwrap_err(),
        Refusal::HashMismatch
    );
    assert_eq!(snapshot(root), before);
}

#[test]
fn refuses_loose_extra_objects() {
    for format in ["sha1", "sha256"] {
        let f = Fixture::with_format("safe", format);
        let receiver = Path::new(&f.repo.root);
        let before = snapshot(receiver);
        let stage = tempfile::tempdir().unwrap();
        init_scratch(receiver, stage.path()).unwrap();
        let odb = receiver.join(".git/objects");
        std::fs::write(
            stage.path().join("objects/info/alternates"),
            odb.to_str().unwrap(),
        )
        .unwrap();
        command(
            stage.path(),
            &["hash-object", "-w", "--stdin"],
            b"extra",
            None,
        )
        .unwrap();
        assert_eq!(
            validate_objects(stage.path(), &f.base, &f.base),
            Err(Refusal::UnexpectedObjects)
        );
        assert_eq!(snapshot(receiver), before);
    }
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
    Fixture::new("multi").import().unwrap();
}
#[test]
fn refuses_lightweight_tag() {
    Fixture::new("lightweight").assert_refusal(Refusal::BadBundle);
}
#[test]
fn requires_both_trailers() {
    for mode in ["plan", "writer"] {
        Fixture::new(mode).assert_refusal(Refusal::MissingTrailer);
    }
}
