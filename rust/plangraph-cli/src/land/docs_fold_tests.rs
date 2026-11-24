use super::docs_fold::*;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Command;

fn write(root: &Path, rel: &str, text: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

fn tree() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    write(dir.path(), "CONTRIBUTING.md", "# Contributing\n\nkeep me\n");
    write(
        dir.path(),
        "docs/KEEPER.md",
        "# Keeper\n\nfirst paragraph\n\nsecond\n",
    );
    write(dir.path(), "docs/SPAWNER.md", "# Spawner\n\nold text\n");
    dir
}

fn no_caps() -> BTreeMap<String, usize> {
    BTreeMap::new()
}

#[test]
fn two_fragments_fold_into_the_generated_reference_and_are_removed() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/ALPHA-ITEM.md", "The alpha behaviour.\n");
    write(
        root,
        "docs/keeper/BETA.md",
        "Beta adds a table.\n\nSecond paragraph.\n",
    );
    let report = fold(root, &no_caps(), true).unwrap();
    assert_eq!(
        report.folded,
        vec!["docs/keeper/BETA.md", "docs/spawner/ALPHA-ITEM.md"]
    );
    assert_eq!(
        read(root, "docs/SPAWNER.md"),
        "# Spawner\n\nold text\n\n<!-- fragment:ALPHA-ITEM -->\nThe alpha behaviour.\n<!-- /fragment:ALPHA-ITEM -->\n"
    );
    assert_eq!(
        read(root, "docs/KEEPER.md"),
        "# Keeper\n\nfirst paragraph\n\nsecond\n\n<!-- fragment:BETA -->\nBeta adds a table.\n\nSecond paragraph.\n<!-- /fragment:BETA -->\n"
    );
    assert_eq!(read(root, "CONTRIBUTING.md"), "# Contributing\n\nkeep me\n");
    assert!(!root.join("docs/spawner/ALPHA-ITEM.md").exists());
    assert!(!root.join("docs/keeper/BETA.md").exists());
}

#[test]
fn order_is_by_name_whatever_the_creation_order() {
    let forward = tree();
    let backward = tree();
    for (root, names) in [
        (forward.path(), ["A-ONE", "B-TWO", "C-THREE"]),
        (backward.path(), ["C-THREE", "B-TWO", "A-ONE"]),
    ] {
        for n in names {
            write(
                root,
                &format!("docs/spawner/{n}.md"),
                &format!("text of {n}\n"),
            );
        }
        fold(root, &no_caps(), true).unwrap();
    }
    assert_eq!(
        read(forward.path(), "docs/SPAWNER.md"),
        read(backward.path(), "docs/SPAWNER.md")
    );
    let text = read(forward.path(), "docs/SPAWNER.md");
    assert!(text.find("A-ONE").unwrap() < text.find("B-TWO").unwrap());
}

#[test]
fn a_second_run_changes_nothing_and_a_crash_before_removal_finishes() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/ITEM.md", "Body.\n");
    fold(root, &no_caps(), true).unwrap();
    let folded = read(root, "docs/SPAWNER.md");
    assert!(fold(root, &no_caps(), true).unwrap().folded.is_empty());
    assert_eq!(read(root, "docs/SPAWNER.md"), folded);
    // The reference was written, the removal never happened.
    write(root, "docs/spawner/ITEM.md", "Body.\n");
    let again = fold(root, &no_caps(), true).unwrap();
    assert_eq!(again.folded, vec!["docs/spawner/ITEM.md"]);
    assert_eq!(read(root, "docs/SPAWNER.md"), folded);
    assert!(!root.join("docs/spawner/ITEM.md").exists());
}

#[test]
fn a_fragment_that_differs_from_the_folded_block_stays() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/ITEM.md", "Body.\n");
    fold(root, &no_caps(), true).unwrap();
    write(root, "docs/spawner/ITEM.md", "Changed body.\n");
    let report = fold(root, &no_caps(), true).unwrap();
    assert!(report.folded.is_empty());
    assert!(report.kept[0].1.contains("different ITEM"), "{report:?}");
    assert!(root.join("docs/spawner/ITEM.md").exists());
}

#[test]
fn other_files_and_names_are_left_alone() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/README.md", "not a fragment\n");
    write(root, "docs/spawner/lower.md", "nor this\n");
    write(root, "docs/keeper/EMPTY.md", "  \n");
    write(root, "docs/other/ITEM.md", "another area\n");
    write(root, "tools/new_code_allowlist.txt", "a\nb\n");
    let report = fold(root, &no_caps(), true).unwrap();
    assert!(report.folded.is_empty());
    assert_eq!(report.kept.len(), 3);
    assert_eq!(read(root, "tools/new_code_allowlist.txt"), "a\nb\n");
    assert!(
        root.join("docs/other/ITEM.md").exists() && root.join("docs/spawner/README.md").exists()
    );
}

#[test]
fn check_mode_writes_nothing() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/ITEM.md", "Body.\n");
    let report = fold(root, &no_caps(), false).unwrap();
    assert_eq!(report.folded, vec!["docs/spawner/ITEM.md"]);
    assert_eq!(read(root, "docs/SPAWNER.md"), "# Spawner\n\nold text\n");
    assert!(root.join("docs/spawner/ITEM.md").exists());
}

#[test]
fn an_oversized_paragraph_is_split_at_sentence_ends() {
    let dir = tree();
    let root = dir.path();
    let sentence = "This sentence says something useful about the landing. ";
    let long: String = sentence.repeat(30);
    write(
        root,
        "docs/spawner/LONG.md",
        &format!("{}\n", long.trim_end()),
    );
    fold(root, &no_caps(), true).unwrap();
    let text = read(root, "docs/SPAWNER.md");
    let body: Vec<&str> = text
        .lines()
        .skip_while(|l| !l.starts_with("<!-- fragment:"))
        .skip(1)
        .take_while(|l| !l.starts_with("<!-- /fragment"))
        .collect();
    assert!(
        body.len() > 1 && body.iter().all(|l| l.chars().count() <= MAX_LINE),
        "{body:?}"
    );
    assert!(
        body.iter().all(|l| l.ends_with('.')),
        "split only at sentence ends: {body:?}"
    );
    assert_eq!(body.join(" "), long.trim_end());
}

#[test]
fn the_line_cap_of_a_reference_is_never_breached() {
    let dir = tree();
    let root = dir.path();
    write(root, "docs/spawner/ITEM.md", "One.\nTwo.\n");
    let ref_lines = read(root, "docs/SPAWNER.md").lines().count();
    // The folded block adds a blank line, two marker lines and two body lines.
    let tight = BTreeMap::from([("docs/SPAWNER.md".to_owned(), ref_lines + 4)]);
    let report = fold(root, &tight, true).unwrap();
    assert!(
        report.folded.is_empty() && report.kept[0].1.contains("cap"),
        "{report:?}"
    );
    assert!(root.join("docs/spawner/ITEM.md").exists());
    assert_eq!(read(root, "docs/SPAWNER.md").lines().count(), ref_lines);
    let roomy = BTreeMap::from([("docs/SPAWNER.md".to_owned(), ref_lines + 5)]);
    assert_eq!(fold(root, &roomy, true).unwrap().folded.len(), 1);
}

#[test]
fn caps_come_from_the_size_checker_less_its_headroom_else_the_limit() {
    let dir = tempfile::tempdir().unwrap();
    write(
        dir.path(),
        "tools/check_file_size.py",
        "LIMIT = 800\nDOC_CAPS = {\"docs/SPAWNER.md\": 728}\nDOC_HEADROOM = 60\n",
    );
    assert_eq!(
        caps(dir.path()),
        BTreeMap::from([
            ("CONTRIBUTING.md".to_owned(), 800),
            ("docs/KEEPER.md".to_owned(), 800),
            ("docs/SPAWNER.md".to_owned(), 668),
        ])
    );
    assert!(caps(tempfile::tempdir().unwrap().path()).is_empty());
    // The checker of this repository is readable by the same pattern, for every reference.
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let real = caps(&repo);
    for (_, reference) in AREAS {
        assert!(real.contains_key(reference), "{reference}");
    }
}

#[test]
fn a_reference_at_the_default_limit_takes_no_fragment() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(root, "tools/check_file_size.py", "LIMIT = 10\n");
    write(root, "CONTRIBUTING.md", &"line\n".repeat(10));
    write(root, "docs/contributing/ITEM.md", "More.\n");
    write(root, "docs/KEEPER.md", "k\n");
    write(root, "docs/SPAWNER.md", "s\n");
    let report = fold(root, &caps(root), true).unwrap();
    assert!(
        report.folded.is_empty() && report.kept[0].1.contains("10-line cap"),
        "{report:?}"
    );
    assert_eq!(read(root, "CONTRIBUTING.md").lines().count(), 10);
}

fn git(root: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn two_branches_with_disjoint_fragments_merge_and_fold_without_direct_edits() {
    let dir = tree();
    let root = dir.path();
    git(root, &["init", "-q", "-b", "main"]);
    git(root, &["add", "-A"]);
    git(root, &["commit", "-qm", "base"]);
    let base = git(root, &["rev-parse", "HEAD"]).trim().to_owned();
    git(root, &["checkout", "-qb", "one"]);
    write(root, "src_one.rs", "fn one() {}\n");
    write(root, "docs/spawner/ONE.md", "Behaviour one.\n");
    git(root, &["add", "-A"]);
    git(root, &["commit", "-qm", "one"]);
    git(root, &["checkout", "-q", "main"]);
    git(root, &["checkout", "-qb", "two"]);
    write(root, "src_two.rs", "fn two() {}\n");
    write(root, "docs/spawner/TWO.md", "Behaviour two.\n");
    git(root, &["add", "-A"]);
    git(root, &["commit", "-qm", "two"]);
    git(root, &["checkout", "-q", "main"]);
    git(
        root,
        &["merge", "-q", "--no-ff", "-m", "train", "one", "two"],
    );
    let before = git(root, &["rev-parse", "HEAD"]).trim().to_owned();
    // No part touched a shared reference, so the train has no direct edit of one.
    assert!(direct_edits(root, &format!("{base}..{before}"))
        .unwrap()
        .iter()
        .all(|(_, n)| *n == 0));
    let report = fold(root, &no_caps(), true).unwrap();
    assert_eq!(
        report.folded,
        vec!["docs/spawner/ONE.md", "docs/spawner/TWO.md"]
    );
    assert_eq!(
        read(root, "docs/SPAWNER.md"),
        "# Spawner\n\nold text\n\n<!-- fragment:ONE -->\nBehaviour one.\n<!-- /fragment:ONE -->\n\n<!-- fragment:TWO -->\nBehaviour two.\n<!-- /fragment:TWO -->\n"
    );
    git(root, &["add", "-A"]);
    git(root, &["commit", "-qm", "fold"]);
    // The fold is the only commit that edits the reference, once for the whole train.
    let edits = direct_edits(root, &format!("{before}..HEAD")).unwrap();
    assert_eq!(
        edits
            .iter()
            .find(|(r, _)| r == "docs/SPAWNER.md")
            .unwrap()
            .1,
        1
    );
    assert_eq!(
        edits.iter().find(|(r, _)| r == "docs/KEEPER.md").unwrap().1,
        0
    );
}

#[test]
fn the_areas_match_the_docs_map_check_when_it_knows_fragments() {
    let check = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/check_changelog_docs.py");
    let text = std::fs::read_to_string(check).unwrap();
    if !text.contains("fragment_docs") {
        return;
    }
    for (area, reference) in AREAS {
        assert!(
            text.contains(&format!("\"{area}\": \"{reference}\"")),
            "{area} -> {reference} is missing from the docs-map check"
        );
    }
}
