use super::*;

fn record(f: &Fixture, path: &str, text: &str) {
    let full = f.tree.join(path);
    std::fs::create_dir_all(full.parent().unwrap()).unwrap();
    std::fs::write(full, text).unwrap();
    git(&f.tree, &["add", "--", path]).unwrap();
    f.commit("fixture\n\nWriter: test");
}

#[test]
fn finalize_file_size_under_limit() {
    let f = Fixture::new();
    record(&f, "small.rs", &"x\n".repeat(400));
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
}

#[test]
fn finalize_file_size_hold_blocks_export_before_content_gate_or_push() {
    struct Never;
    impl ContentGate for Never {
        fn check(&self, _: &RepoEntry, _: &str, _: &str) -> Result<(), WriterError> {
            panic!("publication gate must not be reached");
        }
    }
    for path in ["large.rs", "größer.rs"] {
        let f = Fixture::new();
        record(&f, path, "small");
        let mut checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
        record(&f, path, &"x\n".repeat(401));
        // Simulate a previously issued handle after the checkout changes:
        // export must independently gate the current tree, not trust the handle.
        checked.head_sha = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        let error = check_result(&f.repo, &f.tree, &f.ws).unwrap_err();
        assert!(error
            .to_string()
            .contains(&format!("HOLD gate file-size: {path} 401")));
        let mut blobs =
            crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
        let refs_before = git(Path::new(&f.repo.root), &["show-ref"]).unwrap();
        assert!(matches!(
            export_result(&f.repo, &checked, "test", &Never, &mut blobs),
            Err(WriterError::FileSize(_))
        ));
        assert_eq!(
            git(Path::new(&f.repo.root), &["show-ref"]).unwrap(),
            refs_before
        );
    }
}

#[test]
fn finalize_file_size_exemptions_and_python_agree() {
    let f = Fixture::new();
    record(&f, ".filesize-allow", "allowed.rs generated fixture\n");
    for path in [
        "allowed.rs",
        "vendor/large.rs",
        "data.json",
        "asset.svg",
        "a.lock",
        "a.min.js",
    ] {
        record(&f, path, &"x\n".repeat(401));
    }
    record(&f, "binary", &format!("\0{}", "x\n".repeat(401)));
    record(&f, "unicode.rs", &"x\u{2028}".repeat(400));
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/check_file_size.py");
    let python = || {
        std::process::Command::new("python3")
            .arg(&script)
            .current_dir(&f.tree)
            .env_remove("FILESIZE_REQUIRE_BASE")
            .output()
            .unwrap()
            .status
            .success()
    };
    assert!(python());
    record(&f, "é.rs", &"x\r\n".repeat(401));
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
    assert!(!python());
}
