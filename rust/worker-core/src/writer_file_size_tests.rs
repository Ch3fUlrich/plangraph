use super::*;

fn record(f: &Fixture, path: &str, text: &str) {
    let full = f.tree.join(path);
    std::fs::create_dir_all(full.parent().unwrap()).unwrap();
    std::fs::write(full, text).unwrap();
    git(&f.tree, &["add", "--", path]).unwrap();
    f.commit("fixture\n\nChangelog: skip (file-size fixture)\nWriter: test");
}

#[test]
fn finalize_file_size_under_limit() {
    let f = Fixture::new();
    record(&f, "small.rs", &"x\n".repeat(800));
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
        record(&f, path, &"x\n".repeat(801));
        // Simulate a previously issued handle after the checkout changes:
        // export must independently gate the current tree, not trust the handle.
        checked.head_sha = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        let error = check_result(&f.repo, &f.tree, &f.ws).unwrap_err();
        assert!(error
            .to_string()
            .contains(&format!("HOLD gate file-size: {path} 801")));
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
        record(&f, path, &"x\n".repeat(801));
    }
    record(&f, "binary", &format!("\0{}", "x\n".repeat(801)));
    record(&f, "unicode.rs", &"x\u{2028}".repeat(800));
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    record(&f, "é.rs", &"x\r\n".repeat(801));
    assert_hold(&f, "é.rs 801");
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
    for proposed in [850, 851] {
        let f = Fixture::new();
        record(&f, ".filesize-baseline", "large.rs 850\n");
        let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        record(&f, "large.rs", &"x\n".repeat(850));
        record(&f, ".filesize-baseline", &format!("large.rs {proposed}\n"));
        let head = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
        let result = file_size::check(&f.tree, &base, &head).map_err(WriterError::FileSize);
        if proposed == 850 {
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
    std::fs::write(f.tree.join("invalid.rs"), b"\xff\r\n".repeat(800)).unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nChangelog: skip (file-size fixture)\nWriter: test");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    std::fs::write(f.tree.join("invalid.rs"), b"\xff\r\n".repeat(801)).unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nChangelog: skip (file-size fixture)\nWriter: test");
    assert_hold(&f, "invalid.rs 801");
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

#[test]
fn empty_range_passes_at_head_and_away_from_checkout() {
    let f = Fixture::new();
    record(&f, "large.rs", &"x\n".repeat(801));
    let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    assert!(file_size::check(&f.tree, &base, "HEAD").is_ok());
    record(&f, "later.rs", "small");
    assert!(file_size::check(&f.tree, &base, &base).is_ok());
}

#[test]
fn base_at_checkout_is_not_confused_with_requested_head() {
    let f = Fixture::new();
    record(&f, "later.rs", "original");
    let head = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    record(&f, "later.rs", "small");
    let error = file_size::check(&f.tree, "HEAD", &head).unwrap_err();
    assert!(error.contains("checkout is not at head"), "{error}");
}

#[test]
fn missing_baseline_and_unreadable_baseline_are_distinct() {
    let f = Fixture::new();
    let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    record(&f, ".filesize-baseline", "large.rs 850\n");
    let error = file_size::check(&f.tree, &base, "HEAD").unwrap_err();
    assert_eq!(error, ".filesize-baseline: no baseline at base");
    let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    let blob = git(&f.tree, &["rev-parse", "HEAD:.filesize-baseline"]).unwrap();
    let object = git(
        &f.tree,
        &[
            "rev-parse",
            "--git-path",
            &format!("objects/{}/{}", &blob[..2], &blob[2..]),
        ],
    )
    .unwrap();
    record(&f, "later.rs", "small");
    std::fs::remove_file(f.tree.join(object)).unwrap();
    let error = file_size::check(&f.tree, &base, "HEAD").unwrap_err();
    assert!(error.starts_with("git show failed:"), "{error}");
    assert!(!error.contains("no baseline at base"), "{error}");
}
