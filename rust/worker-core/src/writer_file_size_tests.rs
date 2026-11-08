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
fn finalize_file_size_exemptions_and_decode_fixtures() {
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
    record(&f, "é.rs", &"x\r\n".repeat(401));
    assert_hold(&f, "é.rs 401");
}

fn assert_hold(f: &Fixture, path: &str) {
    let error = check_result(&f.repo, &f.tree, &f.ws).unwrap_err();
    assert!(
        error
            .to_string()
            .contains(&format!("HOLD gate file-size: {path}")),
        "{error}"
    );
}

#[test]
fn baseline_ratchets_against_recorded_base_without_origin_main() {
    for proposed in [450, 451] {
        let f = Fixture::new();
        record(&f, ".filesize-baseline", "large.rs 450\n");
        let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        record(&f, "large.rs", &"x\n".repeat(450));
        record(&f, ".filesize-baseline", &format!("large.rs {proposed}\n"));
        let head = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        let result = file_size::check(&f.tree, &base, &head).map_err(WriterError::FileSize);
        if proposed == 450 {
            assert!(result.is_ok(), "{result:?}");
        } else {
            assert!(result
                .unwrap_err()
                .to_string()
                .contains("HOLD gate file-size: .filesize-baseline: large.rs added or raised"));
        }
    }
}

#[test]
fn invalid_utf8_content_uses_replacement_and_splitlines() {
    let f = Fixture::new();
    record(&f, "invalid.rs", "initial");
    std::fs::write(f.tree.join("invalid.rs"), b"\xff\r\n".repeat(400)).unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nWriter: test");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    std::fs::write(f.tree.join("invalid.rs"), b"\xff\r\n".repeat(401)).unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nWriter: test");
    assert_hold(&f, "invalid.rs 401");
}

#[test]
fn missing_changed_path_and_stale_checkout_hold() {
    let f = Fixture::new();
    record(&f, "missing.rs", "small");
    let head = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    std::fs::remove_file(f.tree.join("missing.rs")).unwrap();
    let error = file_size::check(&f.tree, f.ws.base_sha.as_deref().unwrap(), &head).unwrap_err();
    assert!(
        error.contains("missing.rs: missing changed path"),
        "{error}"
    );
    git(&f.tree, &["restore", "missing.rs"]).unwrap();
    record(&f, "later.rs", "small");
    let error = file_size::check(&f.tree, f.ws.base_sha.as_deref().unwrap(), &head).unwrap_err();
    assert!(
        error.contains("missing.rs: checkout is not at head"),
        "{error}"
    );
}

#[cfg(unix)]
#[test]
fn invalid_utf8_name_is_replacement_decoded_and_missing_holds() {
    use std::os::unix::ffi::OsStrExt;
    let f = Fixture::new();
    std::fs::write(
        f.tree.join(std::ffi::OsStr::from_bytes(b"bad\xff.rs")),
        "small",
    )
    .unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nWriter: test");
    assert_hold(&f, "bad\u{fffd}.rs: missing changed path");
}

#[test]
fn absent_recorded_base_holds_without_fallback() {
    let mut f = Fixture::new();
    record(&f, "small.rs", "small");
    f.ws.base_sha = None;
    assert_hold(&f, "no base");
}
