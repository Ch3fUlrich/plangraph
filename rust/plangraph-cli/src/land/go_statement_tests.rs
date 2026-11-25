use super::go_statement::migration_for;
use super::keeper_schema_tests::python;

const HEAD: &str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";

#[test]
fn the_rust_gate_reads_the_go_row_through_the_shared_python_parser() {
    let row = format!("GO owner/name PR #9 GO-HEAD {HEAD}; MIGRATION 3");
    assert_eq!(
        migration_for(&python, &row, "owner/name", 9, HEAD),
        Ok(Some(3))
    );
    let noted = format!("GO owner/name PR #9 GO-HEAD {HEAD}; MIGRATION-NOTE staged; MIGRATION 3");
    assert_eq!(
        migration_for(&python, &noted, "owner/name", 9, HEAD),
        Ok(Some(3))
    );
    let none = format!("GO #9 GO-HEAD {HEAD}");
    assert_eq!(
        migration_for(&python, &none, "owner/name", 9, HEAD),
        Ok(None)
    );
}

#[test]
fn a_row_for_another_head_or_a_malformed_field_is_refused_with_the_parsers_reason() {
    let row = format!("GO owner/name PR #9 GO-HEAD {HEAD}; MIGRATION 3");
    let other = "b".repeat(40);
    let err = migration_for(&python, &row, "owner/name", 9, &other).unwrap_err();
    assert!(err.contains("not #9 at"), "{err}");
    assert!(migration_for(&python, &row, "owner/name", 10, HEAD).is_err());
    for bad in ["MIGRATION +3", "MIGRATION 03", "MIGRATION  3"] {
        let row = format!("GO owner/name PR #9 GO-HEAD {HEAD}; {bad}");
        let err = migration_for(&python, &row, "owner/name", 9, HEAD).unwrap_err();
        assert!(err.contains("malformed"), "{bad}: {err}");
    }
}
