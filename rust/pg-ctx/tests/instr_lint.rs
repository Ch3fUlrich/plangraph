use pg_ctx::instr_lint::lint;
use std::{fs, path::Path};
use tempfile::{tempdir, TempDir};

fn tree(files: &[(&str, &str)]) -> TempDir {
    let dir = tempdir().unwrap();
    for (path, text) in files {
        let path = dir.path().join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    dir
}

#[test]
fn clean_tree_passes() {
    let dir = tree(&[
        ("CLAUDE.md", "`skills/build/SKILL.md` [guide](docs/guide.md#ok)\n"),
        ("skills/build/SKILL.md", "[local](policy.md) `docs/guide.md`\n"),
        ("skills/build/policy.md", "[cycle](../../CLAUDE.md)\n"),
        ("docs/guide.md", "# ok\n[web](https://example.invalid/no) [anchor](#ok)\n`git commit` `docs/*.md` `https://example.invalid/no`\nNot supersededly or untransitional; until release ships.\n"),
        ("AGENTS.md", "Current instructions.\n"),
        (".git/SKILL.md", "Formerly used. `missing.md`\n"),
        ("docs/unloaded.md", "Formerly used. `missing.md`\n"),
    ]);
    assert!(lint(dir.path(), None).unwrap().is_empty());
}

#[test]
fn history_notes_report_file_and_line() {
    for note in [
        "Superseded",
        "formerly",
        "was previously",
        "transitional",
        "until D-123 migrates",
    ] {
        let dir = tree(&[("CLAUDE.md", &format!("# rules\n{note}\n"))]);
        let hits = lint(dir.path(), None).unwrap();
        assert_eq!(hits.len(), 1, "{note}");
        assert_eq!(
            (&*hits[0].path, hits[0].line, hits[0].check),
            ("CLAUDE.md", 2, "history-note")
        );
    }
}

#[test]
fn dead_markdown_and_backtick_links_report_locations() {
    for link in [
        "[missing](docs/missing.md#anchor)",
        "`docs/missing.md`",
        "`missing.md`",
        "`docs/missing.md:42`",
        "[escape](../missing.md)",
    ] {
        let dir = tree(&[("AGENTS.md", &format!("# rules\n{link}\n"))]);
        let hits = lint(dir.path(), None).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(
            (&*hits[0].path, hits[0].line, hits[0].check),
            ("AGENTS.md", 2, "dead-link")
        );
        assert!(hits[0].message.contains("missing.md"));
    }
}

#[test]
fn discovery_is_transitive_and_output_is_stable() {
    let dir = tree(&[
        ("CLAUDE.md", "[guide](docs/guide.md)\n"),
        ("docs/guide.md", "[next](next.md)\n"),
        ("docs/next.md", "Formerly used.\n"),
        (".claude/AGENTS.md", "Transitional.\n"),
        ("skills/z/SKILL.md", "Superseded.\n`gone.md`\n"),
    ]);
    let hits = lint(dir.path(), None).unwrap();
    assert_eq!(hits.len(), 4);
    assert_eq!(
        hits.iter().map(|h| h.path.as_str()).collect::<Vec<_>>(),
        [
            ".claude/AGENTS.md",
            "docs/next.md",
            "skills/z/SKILL.md",
            "skills/z/SKILL.md"
        ]
    );
    assert_eq!(hits, lint(dir.path(), None).unwrap());
}

#[test]
fn allowlisted_hits_pass_but_growth_fails() {
    let dir = tree(&[
        ("CLAUDE.md", "Formerly used.\n`gone.md`\n"),
        ("allow", "CLAUDE.md|history-note|1\nCLAUDE.md|dead-link|1\n"),
    ]);
    let allow = Some(Path::new("allow"));
    assert!(lint(dir.path(), allow).unwrap().is_empty());
    fs::write(
        dir.path().join("CLAUDE.md"),
        "Formerly used.\n`gone.md`\nTransitional.\n",
    )
    .unwrap();
    let hits = lint(dir.path(), allow).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!((hits[0].check, hits[0].line), ("history-note", 3));
}

#[test]
fn stale_allowlist_fails_even_for_removed_files() {
    for (text, count) in [("Formerly used.\n", 2), ("Current.\n", 1)] {
        let dir = tree(&[
            ("CLAUDE.md", text),
            (
                "allow",
                &format!("CLAUDE.md|history-note|{count}\nmissing.md|dead-link|1\n"),
            ),
        ]);
        let hits = lint(dir.path(), Some(Path::new("allow"))).unwrap();
        assert_eq!(hits.len(), 2);
        assert!(hits
            .iter()
            .all(|h| h.path == "allow" && h.check == "stale-allowlist"));
        assert_eq!((hits[0].line, hits[1].line), (1, 2));
    }
}

#[test]
fn malformed_allowlist_is_not_silently_accepted() {
    for entry in [
        "CLAUDE.md|history-note|no",
        "CLAUDE.md|unknown|1",
        "CLAUDE.md|dead-link|1\nCLAUDE.md|dead-link|1",
    ] {
        let dir = tree(&[("allow", entry)]);
        assert!(lint(dir.path(), Some(Path::new("allow"))).is_err());
    }
}
