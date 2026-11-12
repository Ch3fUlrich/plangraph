use super::*;

#[test]
fn writer_docs_map_shared_parity() {
    let cases: serde_json::Value =
        serde_json::from_str(include_str!("../../../tests/fixtures/writer_docs_map.json")).unwrap();
    for case in cases["globs"].as_array().unwrap() {
        let rx = docs_map::glob_to_regex(case[0].as_str().unwrap()).unwrap();
        assert_eq!(
            rx.is_match(case[1].as_str().unwrap()),
            case[2].as_bool().unwrap(),
            "{case}"
        );
    }
    for case in cases["maps"].as_array().unwrap() {
        let (date, rules, errors) = docs_map::parse_docs_map(case[0].as_str().unwrap());
        assert_eq!(
            serde_json::json!(date.map(|d| d.to_string())),
            case[1],
            "{case}"
        );
        assert_eq!(serde_json::json!(rules), case[2], "{case}");
        let classes: Vec<_> = errors
            .iter()
            .map(|e| e.split_once(": ").unwrap().1.split(':').next().unwrap())
            .collect();
        assert_eq!(serde_json::json!(classes), case[3], "{case}");
    }
}

fn record(f: &Fixture, files: &[(&str, &str)]) {
    for (path, text) in files {
        let full = f.tree.join(path);
        std::fs::create_dir_all(full.parent().unwrap()).unwrap();
        std::fs::write(full, text).unwrap();
    }
    git(&f.tree, &["add", "."]).unwrap();
    f.commit("fixture\n\nChangelog: skip (docs-map test)\nWriter: test");
}

#[test]
fn finalize_docs_map_pass_missing_and_alternative_doc() {
    for files in [
        vec![("src/a.rs", "code")],
        vec![
            (".docs-map", "src/** -> docs/a.md, docs/b.md"),
            ("src/a.rs", "code"),
            ("docs/b.md", "doc"),
        ],
    ] {
        let f = Fixture::new();
        record(&f, &files);
        assert!(check_result(&f.repo, &f.tree, &f.ws).is_ok());
    }
}

#[test]
fn finalize_docs_map_hold_names_all_pairs_and_blocks_export() {
    struct Never;
    impl ContentGate for Never {
        fn check(&self, _: &RepoEntry, _: &str, _: &str) -> Result<(), WriterError> {
            panic!("publication must not run on HOLD");
        }
    }
    let f = Fixture::new();
    f.commit("safe\n\nWriter: test");
    let checked = check_result(&f.repo, &f.tree, &f.ws).unwrap();
    record(
        &f,
        &[
            (
                ".docs-map",
                "src/** -> docs/a.md, docs/b.md\nlib/* -> docs/lib.md",
            ),
            ("src/a.rs", "code"),
            ("lib/b.rs", "code"),
        ],
    );
    let error = check_result(&f.repo, &f.tree, &f.ws)
        .unwrap_err()
        .to_string();
    assert!(error.starts_with("HOLD gate docs-map:"), "{error}");
    assert!(
        error.contains("src/a.rs -> docs/a.md, docs/b.md"),
        "{error}"
    );
    assert!(error.contains("lib/b.rs -> docs/lib.md"), "{error}");
    let refs = git(Path::new(&f.repo.root), &["show-ref"]).unwrap();
    let mut blobs = crate::writer_blob::LocalBlobStore::open(&f._dir.path().join("blobs")).unwrap();
    let error = export_result(&f.repo, &checked, "docs-map", &Never, &mut blobs).unwrap_err();
    assert!(error.to_string().starts_with("HOLD gate docs-map:"));
    assert_eq!(git(Path::new(&f.repo.root), &["show-ref"]).unwrap(), refs);
}

#[test]
fn finalize_docs_map_malformed_and_enforcement_dates() {
    for (text, passes, detail) in [
        (
            "# comment\nbroken rule",
            false,
            ".docs-map:2: malformed rule",
        ),
        (
            "# enforce-after: invalid",
            false,
            "malformed enforce-after date",
        ),
        (
            "# enforce-after: 2000-01-01\nsrc/** -> docs/a.md",
            false,
            "src/a.rs -> docs/a.md",
        ),
        ("# enforce-after: 9999-12-31\nsrc/** -> docs/a.md", true, ""),
    ] {
        let f = Fixture::new();
        record(&f, &[(".docs-map", text), ("src/a.rs", "code")]);
        let result = check_result(&f.repo, &f.tree, &f.ws);
        assert_eq!(result.is_ok(), passes, "{result:?}");
        if !passes {
            assert!(result.unwrap_err().to_string().contains(detail));
        }
    }
}

#[test]
fn writer_docs_map_uses_committed_head_and_merge_base_with_date_boundary() {
    let f = Fixture::new();
    record(
        &f,
        &[(
            ".docs-map",
            "# enforce-after: 2026-11-08\nsrc/** -> docs/a.md",
        )],
    );
    let base = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    record(&f, &[("src/a.rs", "code")]);
    let head = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    git(&f.tree, &["checkout", "-b", "lane/other", &base]).unwrap();
    record(
        &f,
        &[
            (".docs-map", "# not the head policy"),
            ("docs/a.md", "outside range"),
        ],
    );
    let other = git(&f.tree, &["rev-parse", "HEAD"]).unwrap();
    for (day, passes) in [(7, true), (8, false), (9, false)] {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, day).unwrap();
        assert_eq!(
            docs_map::check_on(&f.tree, &other, &head, today).is_ok(),
            passes
        );
    }
    assert!(docs_map::check(&f.tree, "missing-ref", &head).is_err());
}
