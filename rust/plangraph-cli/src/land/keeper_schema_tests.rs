use super::build_tests::{g, Fx};
use super::keeper_schema::*;

/// The Python of this checkout: the one registry reader the gates share.
pub(super) fn python() -> std::process::Command {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    crate::base_fresh::python_command(Some(&root))
}

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
fn a_rise_names_the_head_version_and_equal_or_absent_files_are_none() {
    let fx = Fx::new();
    let two = commit(&fx, Some(ONE));
    let three = commit(&fx, Some(THREE));
    let none = commit(&fx, None);
    let p = fx.path();
    assert_eq!(rise(p, &two, &three, &python), Ok(Some(3)));
    assert_eq!(rise(p, &two, &two, &python), Ok(None));
    assert_eq!(rise(p, &none, &two, &python), Ok(Some(2)));
    assert!(rise(p, &three, &two, &python)
        .unwrap_err()
        .contains("regressed"));
    assert_eq!(at(p, &none, &python), Ok(0));
}

#[test]
fn a_revision_that_is_not_a_commit_is_refused_not_read_as_version_zero() {
    let fx = Fx::new();
    let good = commit(&fx, Some(ONE));
    let bad = "f".repeat(40);
    assert!(at(fx.path(), &bad, &python)
        .unwrap_err()
        .contains("not a commit"));
    assert!(rise(fx.path(), &bad, &good, &python).is_err());
    assert!(rise(fx.path(), &good, &bad, &python).is_err());
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
        run_cli(&flags(&two, &three), &python).unwrap(),
        [r#"{"keeper_schema":{"base":2,"head":3},"migration":null}"#]
    );
    assert!(run_cli(&flags(&two, &"0".repeat(40)), &python).is_err());
    let missing = std::collections::BTreeMap::from([("repo".to_string(), ".".to_string())]);
    assert!(run_cli(&missing, &python)
        .unwrap_err()
        .contains("missing --head"));
}

#[test]
fn the_rust_gate_reads_the_registry_through_the_shared_reader_whatever_the_python_syntax() {
    let real = "MIGRATIONS = (Migration(1, a), Migration(2, b))\n";
    for ok in [
        format!("\"\"\"Shape:\nMIGRATIONS = (Migration(1, a),)\n\"\"\"\n{real}"),
        "MIGRATIONS=(Migration(1, a),\n Migration(2, b))\n".to_string(),
    ] {
        assert_eq!(parse_latest(&python, &ok), Ok(2), "{ok}");
    }
    let dup = "MIGRATIONS = (Migration(1, a),)\nX = (1,\n  2); MIGRATIONS = (Migration(1, a), Migration(2, b))\n";
    assert!(parse_latest(&python, dup).is_err());
    assert!(parse_latest(&python, "MIGRATIONS = (Migration(1, a), Migration(3, c))\n").is_err());
    assert!(parse_latest(&python, "x = 1\n").is_err());
    // Writes the static read cannot account for are refused, so the keeper never loads another registry.
    for bad in [
        format!("{real}MIGRATIONS += (Migration(3, c),)\n"),
        format!("{real}MIGRATIONS: tuple = (Migration(1, a), Migration(2, b), Migration(3, c))\n"),
        format!("if False: MIGRATIONS = (Migration(1, a),)\n{real}"),
    ] {
        assert!(parse_latest(&python, &bad).is_err(), "{bad}");
    }
}
