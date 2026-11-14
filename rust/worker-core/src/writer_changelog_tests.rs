use super::*;

const LEDGER: &str = "CHANGELOG.md";

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
                ("docs/guide.txt", "docs"),
                ("README.md", "docs"),
                ("LICENSE", "license"),
                (".docs-map", "# mapping"),
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
    record(&f, &[("changelog.d/entry.md", "- Existing\n")], "");
    f.ws.base_sha = Some(git(&f.tree, &["rev-parse", "HEAD"]).unwrap());
    f.ws.revision_sha = f.ws.base_sha.clone();
    record(&f, &[("code.rs", "x")], "");
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_err());
    record(
        &f,
        &[("changelog.d/entry.md", "- Existing\n- Existing\n")],
        "",
    );
    assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
}

#[test]
fn parity_direct_changelog_requires_release_and_code_still_needs_fragment() {
    for (trailer, code, fragment, passes) in [
        ("", false, false, false),
        ("", true, true, false),
        ("Changelog: skip (docs)", false, false, false),
        ("Release:", false, false, false),
        ("release: 1.2.3", false, false, true),
        ("Release: 1.2.3", true, false, false),
        ("Release: 1.2.3", true, true, true),
    ] {
        let f = Fixture::new();
        let mut files = vec![("CHANGELOG.md", "## Unreleased\n- direct edit\n")];
        if code {
            files.push(("code.rs", "x"));
        }
        if fragment {
            files.push(("changelog.d/entry.md", "- entry"));
        }
        record(&f, &files, trailer);
        assert_python_parity(&f, passes);
    }
}

#[test]
fn parity_release_fold_commit_passes() {
    let mut f = Fixture::new();
    record(
        &f,
        &[
            ("CHANGELOG.md", "## Unreleased\n"),
            ("changelog.d/entry.md", "- folded entry\n"),
        ],
        "Release: seed fixture",
    );
    f.ws.base_sha = Some(git(&f.tree, &["rev-parse", "HEAD"]).unwrap());
    f.ws.revision_sha = f.ws.base_sha.clone();
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/changelog_fold.py");
    assert!(std::process::Command::new("python3")
        .arg(script)
        .current_dir(&f.tree)
        .status()
        .unwrap()
        .success());
    git(&f.tree, &["add", "-A"]).unwrap();
    f.commit("fold release\n\nRelease: 1.2.3\nWriter: test");
    assert_python_parity(&f, true);
}

#[test]
fn parity_later_release_does_not_excuse_ordinary_edit() {
    let f = Fixture::new();
    record(&f, &[("CHANGELOG.md", "- ordinary edit")], "");
    record(&f, &[("CHANGELOG.md", "- folded release")], "Release: 1.0");
    assert_python_parity(&f, false);
}

#[test]
fn parity_release_text_in_body_does_not_qualify() {
    let f = Fixture::new();
    std::fs::write(f.tree.join(LEDGER), "- edit").unwrap();
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nRelease: 1.0\n\nBody prose follows.\n\nWriter: test");
    assert_python_parity(&f, false);
}

#[test]
fn parity_merge_resolution_cannot_bypass_release_rule() {
    let f = Fixture::new();
    git(&f.tree, &["checkout", "-b", "feature"]).unwrap();
    record(&f, &[("docs/a.md", "a")], "");
    git(&f.tree, &["checkout", &f.ws.branch.clone().unwrap()]).unwrap();
    record(&f, &[("docs/b.md", "b")], "");
    git(&f.tree, &["merge", "--no-ff", "--no-commit", "feature"]).unwrap();
    record(&f, &[("CHANGELOG.md", "- sneaked into merge")], "");
    assert_python_parity(&f, false);
}

#[test]
fn parity_merge_inherits_release_lines_but_cannot_discard_them() {
    for (resolution, release, passes) in [
        ("preserve", false, true),
        ("ours", false, false),
        ("theirs", false, false),
        ("drop_common", false, false),
        ("invent", false, false),
        ("ours", true, true),
    ] {
        let mut f = Fixture::new();
        let ledger = "## Unreleased\n- existing\n";
        record(
            &f,
            &[
                ("CHANGELOG.md", ledger),
                (".gitattributes", "CHANGELOG.md merge=union\n"),
            ],
            "Release: seed",
        );
        f.ws.base_sha = Some(git(&f.tree, &["rev-parse", "HEAD"]).unwrap());
        f.ws.revision_sha = f.ws.base_sha.clone();
        git(&f.tree, &["checkout", "-b", "feature"]).unwrap();
        record(
            &f,
            &[("CHANGELOG.md", &format!("{ledger}- feature\n"))],
            "Release: 1.0",
        );
        git(&f.tree, &["checkout", f.ws.branch.as_ref().unwrap()]).unwrap();
        record(
            &f,
            &[("CHANGELOG.md", &format!("{ledger}- main\n"))],
            "Release: 2.0",
        );
        git(&f.tree, &["merge", "--no-ff", "--no-commit", "feature"]).unwrap();
        let merged = std::fs::read_to_string(f.tree.join(LEDGER)).unwrap();
        assert!(merged.contains("- main\n") && merged.contains("- feature\n"));
        match resolution {
            "ours" | "theirs" => {
                git(
                    &f.tree,
                    &[
                        "checkout",
                        if resolution == "ours" {
                            "HEAD"
                        } else {
                            "feature"
                        },
                        "--",
                        "CHANGELOG.md",
                    ],
                )
                .unwrap();
            }
            "drop_common" => {
                std::fs::write(f.tree.join(LEDGER), merged.replace("- existing\n", "")).unwrap()
            }
            "invent" => {
                std::fs::write(f.tree.join(LEDGER), format!("{merged}- invented\n")).unwrap()
            }
            _ => {}
        }
        git(&f.tree, &["add", "."]).unwrap();
        f.commit(if release {
            "merge\n\nRelease: 3.0\nWriter: test"
        } else {
            "merge\n\nWriter: test"
        });
        assert_python_parity(&f, passes);
    }
}
