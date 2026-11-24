use super::build_tests::{g, Fx};
use super::keeper_schema::*;

const ONE: &str = "MIGRATIONS = (Migration(1, \"a\", X),\n              Migration(2, \"b\", Y))\n";
const THREE: &str = "class Migration(tuple):\n    pass\nMIGRATIONS = (Migration(1, \"a\", X), Migration(2, \"b\", Y),\n  Migration(3, \"c\", Z))\n";

fn commit(fx: &Fx, text: Option<&str>) -> String {
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", &fx.base()]);
    if let Some(text) = text {
        let path = p.join(SCHEMA_PATH);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    g(p, &["add", "-A"]);
    g(p, &["commit", "-q", "--allow-empty", "-m", "c"]);
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    head
}

#[test]
fn the_version_counts_registry_entries_and_ignores_the_class_line() {
    assert_eq!(parse_latest(ONE), Ok(2));
    assert_eq!(parse_latest(THREE), Ok(3));
    assert!(parse_latest("nothing here").is_err());
    assert!(parse_latest("MIGRATIONS = (Migration(1, a), Migration(3, c))").is_err());
    assert!(parse_latest("MIGRATIONS = (Migration(1, a), Migration(1, a))").is_err());
}

#[test]
fn a_rise_names_the_head_version_and_equal_or_absent_files_are_none() {
    let fx = Fx::new();
    let two = commit(&fx, Some(ONE));
    let three = commit(&fx, Some(THREE));
    let none = commit(&fx, None);
    let p = fx.path();
    assert_eq!(rise(p, &two, &three), Ok(Some(3)));
    assert_eq!(rise(p, &two, &two), Ok(None));
    assert_eq!(rise(p, &none, &two), Ok(Some(2)));
    assert!(rise(p, &three, &two).unwrap_err().contains("regressed"));
    assert_eq!(at(p, &none), Ok(0));
}

#[test]
fn only_direct_registry_entries_count_and_strings_comments_and_other_mentions_are_skipped() {
    let text = "# MIGRATIONS = (Migration(1, a),)\nclass Migration(tuple): pass\nOTHER = (Migration(7, z),)\nMIGRATIONS = (Migration(1, \"Migration(9, x) )\"),\n  # Migration(8, c)\n  Migration(2, '''Migration(4, q)\n)''', X),)\nMore = (Migration(3, y),)\n";
    assert_eq!(parse_latest(text), Ok(2));
    assert!(parse_latest("MIGRATIONS = (Migration(1, a), g(2))\n").is_err());
    assert!(parse_latest("MIGRATIONS = (Migration(1, a),\n").is_err());
    assert!(parse_latest("x = 1\n").is_err());
}

#[test]
fn multibyte_characters_in_comments_and_strings_do_not_panic_the_scan() {
    let text = "MIGRATIONS = (Migration(1, \"héllo — ✓\"), # ünï ✓\n  Migration(2, 'a\\é', X))\n";
    assert_eq!(parse_latest(text), Ok(2));
    assert!(parse_latest("MIGRATIONS = (Migration(1, \"é\"),\n").is_err());
}

#[test]
fn a_revision_that_is_not_a_commit_is_refused_not_read_as_version_zero() {
    let fx = Fx::new();
    let good = commit(&fx, Some(ONE));
    let bad = "f".repeat(40);
    assert!(at(fx.path(), &bad).unwrap_err().contains("not a commit"));
    assert!(rise(fx.path(), &bad, &good).is_err());
    assert!(rise(fx.path(), &good, &bad).is_err());
}

#[test]
fn the_keeper_schema_command_prints_both_versions_and_refuses_a_bad_revision() {
    let fx = Fx::new();
    let two = commit(&fx, Some(ONE));
    let three = commit(&fx, Some(THREE));
    let flags = |base: &str, head: &str| {
        std::collections::BTreeMap::from([
            ("repo".to_string(), fx.path().display().to_string()),
            ("base".to_string(), base.to_string()),
            ("head".to_string(), head.to_string()),
        ])
    };
    assert_eq!(
        run_cli(&flags(&two, &three)).unwrap(),
        [r#"{"keeper_schema":{"base":2,"head":3},"migration":null}"#]
    );
    assert!(run_cli(&flags(&two, &"0".repeat(40))).is_err());
    let missing = std::collections::BTreeMap::from([("repo".to_string(), ".".to_string())]);
    assert!(run_cli(&missing).unwrap_err().contains("missing --head"));
}

#[test]
fn a_registry_example_in_a_docstring_or_comment_is_not_the_registry() {
    let real = "MIGRATIONS = (Migration(1, a), Migration(2, b))\n";
    let doc = format!("\"\"\"Shape:\nMIGRATIONS = (Migration(1, a),)\n\"\"\"\n{real}");
    assert_eq!(parse_latest(&doc), Ok(2));
    let single = format!("'''\nMIGRATIONS = (Migration(1, a),)\n'''\n{real}");
    assert_eq!(parse_latest(&single), Ok(2));
    let comment = format!("# MIGRATIONS = (Migration(1, a),)\n{real}");
    assert_eq!(parse_latest(&comment), Ok(2));
    assert!(parse_latest("\"\"\"\nMIGRATIONS = (Migration(1, a),)\n\"\"\"\n").is_err());
    let twice = format!("MIGRATIONS = (Migration(1, a),)\n{real}");
    assert!(parse_latest(&twice).is_err());
}

#[test]
fn the_registry_is_found_whatever_its_spacing_and_a_duplicate_is_refused() {
    let one = "MIGRATIONS=(Migration(1, a),\n  Migration(2, b))\n";
    assert_eq!(parse_latest(one), Ok(2));
    assert_eq!(parse_latest("MIGRATIONS  =\t[Migration(1, a)]\n"), Ok(1));
    let twice = "MIGRATIONS = (Migration(1, a),)\nMIGRATIONS=(Migration(1, a), Migration(2, b))\n";
    assert!(parse_latest(twice).is_err());
    assert!(parse_latest("MIGRATIONS = tuple(load())\n").is_err());
    assert!(parse_latest("MIGRATIONS == (Migration(1, a),)\nOTHER = 1\n").is_err());
    assert!(parse_latest("MIGRATIONS = (Migration(1, a), [Migration(2, b)])\n").is_err());
}
