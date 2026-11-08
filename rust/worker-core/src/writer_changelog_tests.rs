use super::*;

fn assert_python_parity(f: &Fixture, passes: bool) {
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/check_changelog_docs.py");
    let python = std::process::Command::new("python3")
        .arg(script)
        .args(["--cwd", f.tree.to_str().unwrap(), "--base"])
        .arg(f.ws.base_sha.as_ref().unwrap())
        .args(["--head", "HEAD"])
        .output()
        .unwrap();
    assert_eq!(
        python.status.success(),
        passes,
        "{}{}",
        String::from_utf8_lossy(&python.stdout),
        String::from_utf8_lossy(&python.stderr)
    );
    let rust = check_result(&f.repo, &f.tree, &f.ws);
    if passes {
        assert!(rust.is_ok(), "{rust:?}");
    } else {
        assert!(matches!(rust, Err(WriterError::Changelog(_))), "{rust:?}");
    }
}

#[test]
fn parity_doc_only_and_empty_commit_skip_without_reason_pass() {
    for files in [vec![("README.md", "docs")], vec![]] {
        let f = Fixture::new();
        record(&f, &files, "Changelog: skip");
        assert_python_parity(&f, true);
    }
}

#[test]
fn parity_indented_and_irregular_section_heading_hold() {
    for heading in ["  ## Released", "##\tReleased", "  ##\tReleased"] {
        let mut f = Fixture::new();
        record(
            &f,
            &[(
                "CHANGELOG.md",
                &format!("## Unreleased\n{heading}\n- Old\n"),
            )],
            "",
        );
        f.ws.base_sha = Some(git(&f.tree, &["rev-parse", "HEAD"]).unwrap());
        f.ws.revision_sha = f.ws.base_sha.clone();
        record(
            &f,
            &[
                ("code.rs", "x"),
                (
                    "CHANGELOG.md",
                    &format!("## Unreleased\n{heading}\n- Old\n- Old\n"),
                ),
            ],
            "",
        );
        assert_python_parity(&f, false);
    }
}

#[test]
fn parity_heading_only_fragment_and_dated_unreleased_hold() {
    for (path, text) in [
        ("changelog.d/entry.md", "# Title\n### Heading\n"),
        ("CHANGELOG.md", "## Unreleased - 2026-11-08\n- Entry\n"),
    ] {
        let f = Fixture::new();
        record(&f, &[("code.rs", "x"), (path, text)], "");
        assert_python_parity(&f, false);
    }
}

#[test]
fn parity_skip_reason_forms() {
    for (trailer, passes) in [
        ("Changelog: skip", false),
        ("Changelog: skip ()", false),
        ("Changelog: skip ( )", false),
        ("Changelog: skip reason", false),
        ("Changelog: skip (reason)", true),
        ("Changelog: skip (reason with ) inside)", true),
        ("Changelog: skip ((nested))", true),
        ("Changelog: skip (reason) trailing", false),
        ("cHaNgElOg: SKIP ( reason )", true),
        ("Changelog: skip (valid)\nChangelog: skip", false),
    ] {
        let f = Fixture::new();
        record(&f, &[("code.rs", "x")], trailer);
        assert_python_parity(&f, passes);
    }
}

fn record(f: &Fixture, files: &[(&str, &str)], trailer: &str) {
    for (path, text) in files {
        let full = f.tree.join(path);
        std::fs::create_dir_all(full.parent().unwrap()).unwrap();
        std::fs::write(full, text).unwrap();
    }
    git(&f.tree, &["add", "."]).unwrap();
    f.commit(&format!("fixture\n\n{trailer}\nWriter: test"));
}

#[test]
fn finalize_changelog_pass_cases() {
    for (files, trailer) in [
        (
            vec![
                ("code.rs", "x"),
                ("changelog.d/entry.md", "# Title\nNew entry"),
            ],
            "",
        ),
        (
            vec![
                ("code.rs", "x"),
                (
                    "CHANGELOG.md",
                    "## [Unreleased]\n- New entry\n## Released\nold",
                ),
            ],
            "",
        ),
        (
            vec![
                ("docs/guide.txt", "docs"),
                ("README.md", "docs"),
                ("LICENSE", "license"),
                (".docs-map", "mapping"),
            ],
            "",
        ),
        (vec![("code.rs", "x")], "Changelog: skip (test-only change)"),
    ] {
        let f = Fixture::new();
        record(&f, &files, trailer);
        assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    }
}

#[test]
fn finalize_changelog_hold_blocks_export_and_push() {
    struct Never;
    impl ContentGate for Never {
        fn check(&self, _: &RepoEntry, _: &str, _: &str) -> Result<(), WriterError> {
            panic!("publication must not be attempted on HOLD");
        }
    }
    let f = Fixture::new();
    f.commit("safe\n\nWriter: test");
    let mut checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    record(&f, &[("code.rs", "x")], "");
    checked.head_sha = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    let error = check_result(&f.repo, &f.tree, &f.ws).unwrap_err();
    assert!(error.to_string().starts_with("HOLD gate changelog:"));
    let refs = git(Path::new(&f.repo.root), &["show-ref"]).unwrap();
    let mut blobs = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    assert!(matches!(
        export_result(&f.repo, &checked, "test", &Never, &mut blobs),
        Err(WriterError::Changelog(_))
    ));
    assert_eq!(git(Path::new(&f.repo.root), &["show-ref"]).unwrap(), refs);
}

#[test]
fn finalize_changelog_rejects_invalid_or_partial_skips_and_nonentries() {
    for trailer in ["Changelog: skip", "Changelog: skip ( )", ""] {
        let f = Fixture::new();
        record(
            &f,
            &[
                ("code.rs", "x"),
                ("changelog.d/README.md", "not an entry"),
                ("changelog.d/nested/a.md", "not a fragment"),
                (
                    "CHANGELOG.md",
                    "## Unreleased\n### Heading\n## Released\nentry",
                ),
            ],
            trailer,
        );
        assert!(matches!(
            check_result(&f.repo, &f.tree, &f.ws),
            Err(WriterError::Changelog(_))
        ));
    }
    let f = Fixture::new();
    record(&f, &[("one.rs", "x")], "Changelog: skip (first)");
    record(&f, &[("two.rs", "x")], "");
    assert!(matches!(
        check_result(&f.repo, &f.tree, &f.ws),
        Err(WriterError::Changelog(_))
    ));
}

#[test]
fn finalize_changelog_counts_entries_against_base() {
    let mut f = Fixture::new();
    record(&f, &[("CHANGELOG.md", "## Unreleased\n- Existing\n")], "");
    f.ws.base_sha = Some(git(&f.tree, &["rev-parse", "HEAD"]).unwrap());
    f.ws.revision_sha = f.ws.base_sha.clone();
    record(&f, &[("code.rs", "x")], "");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
    record(
        &f,
        &[("CHANGELOG.md", "## Unreleased\n- Existing\n- Existing\n")],
        "",
    );
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
}
