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
    assert_eq!(parse_latest("nothing here"), Ok(0));
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
    assert_eq!(parse_latest("x = 1\n"), Ok(0));
}
