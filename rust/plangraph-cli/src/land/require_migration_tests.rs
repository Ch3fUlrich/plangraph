use super::build_tests::Fx;
use super::keeper_schema_tests::{commit, python};
use super::require_migration::*;
use std::collections::BTreeMap;

const TWO: &str = "MIGRATIONS = (Migration(1, a), Migration(2, b))\n";
const THREE: &str = "MIGRATIONS = (Migration(1, a), Migration(2, b), Migration(3, c))\n";

fn refused(fx: &Fx, base: &str, head: &str, body: &str) -> Result<String, String> {
    check(fx.path(), base, head, body, &python)
}

#[test]
fn a_schema_rise_is_refused_without_its_migration_line_and_accepted_with_it() {
    let fx = Fx::new();
    let (base, head) = (commit(&fx, Some(TWO)), commit(&fx, Some(THREE)));
    let err = refused(&fx, &base, &head, "a train\n\nGO-KEYS [a]\n").unwrap_err();
    assert!(
        err.contains("MIGRATION 3") && err.contains("keeper-restart"),
        "{err}"
    );
    for wrong in [
        "MIGRATION 2",
        "MIGRATION +3",
        "MIGRATION  3",
        "MIGRATION 03",
        "note MIGRATION 3 note",
    ] {
        assert!(
            refused(&fx, &base, &head, &format!("x\n{wrong}\n")).is_err(),
            "{wrong}"
        );
    }
    let twice = "MIGRATION 3\nMIGRATION 3\n";
    assert!(refused(&fx, &base, &head, twice).is_err());
    assert!(refused(&fx, &base, &head, "x\nMIGRATION 3\ny\n").is_ok());
    assert!(refused(&fx, &base, &head, "x\r\nMIGRATION 3\r\n").is_ok());
}

#[test]
fn no_rise_needs_no_line_and_a_bad_revision_is_refused() {
    let fx = Fx::new();
    let (two, same) = (
        commit(&fx, Some(TWO)),
        commit(&fx, Some(&format!("# again\n{TWO}"))),
    );
    assert!(refused(&fx, &two, &same, "no field here").is_ok());
    assert!(refused(&fx, &two, &"0".repeat(40), "MIGRATION 3").is_err());
    // A body that names a migration with no rise is not an error either.
    assert!(refused(&fx, &two, &same, "MIGRATION 3").is_ok());
}

#[test]
fn the_command_reads_the_body_file() {
    let fx = Fx::new();
    let (base, head) = (commit(&fx, Some(TWO)), commit(&fx, Some(THREE)));
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("body.md");
    let flags = |body: &str| {
        std::fs::write(&file, body).unwrap();
        BTreeMap::from([
            ("repo".to_string(), fx.path().display().to_string()),
            ("base".to_string(), base.clone()),
            ("head".to_string(), head.clone()),
            ("body-file".to_string(), file.display().to_string()),
        ])
    };
    assert!(run_cli(&flags("nothing\n"), &python).is_err());
    assert!(run_cli(&flags("MIGRATION 3\n"), &python).is_ok());
    let mut missing = flags("x");
    missing.remove("body-file");
    assert!(run_cli(&missing, &python)
        .unwrap_err()
        .contains("missing --body-file"));
}
