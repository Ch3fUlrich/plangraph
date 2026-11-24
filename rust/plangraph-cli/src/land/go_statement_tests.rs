use super::go_statement::*;

const HEAD: &str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";

#[test]
fn the_field_grammar_is_the_plain_decimal_the_python_gate_requires() {
    assert_eq!(migration_field("GO #9; MIGRATION 3; PLAN x"), Ok(Some(3)));
    assert_eq!(migration_field("GO #9"), Ok(None));
    assert_eq!(migration_field("MIGRATION 3 GO #9"), Ok(None));
    // Fields of another name, or a bare marker, are not the migration field (the Python gate reads the same).
    assert_eq!(
        migration_field("GO #9; MIGRATION-NOTE staged; MIGRATION 3"),
        Ok(Some(3))
    );
    assert_eq!(migration_field("GO #9; MIGRATION-NOTE staged"), Ok(None));
    assert_eq!(migration_field("GO #9; MIGRATION"), Ok(None));
    for bad in [
        "GO #9; MIGRATION +3",
        "GO #9; MIGRATION 03",
        "GO #9; MIGRATION  3",
        "GO #9; MIGRATION 0",
        "GO #9; MIGRATION three",
        "GO #9; MIGRATION 3; MIGRATION 3",
    ] {
        assert!(migration_field(bad).is_err(), "{bad}");
    }
}

#[test]
fn a_row_for_another_pull_request_or_head_supplies_no_evidence() {
    let row = format!("GO owner/name PR #9 GO-HEAD {HEAD}; MIGRATION 3");
    assert_eq!(migration_for(&row, "owner/name", 9, HEAD), Ok(Some(3)));
    assert_eq!(
        migration_for(&format!("GO #9 GO-HEAD {HEAD}"), "owner/name", 9, HEAD),
        Ok(None)
    );
    let other = "b".repeat(40);
    assert!(migration_for(&row, "owner/name", 9, &other).is_err());
    assert!(migration_for(&row, "owner/name", 10, HEAD).is_err());
    assert!(migration_for("GO #9; MIGRATION 3", "owner/name", 9, HEAD).is_err());
    let two = format!("GO #9 GO-HEAD {HEAD} GO-HEAD {HEAD}; MIGRATION 3");
    assert!(migration_for(&two, "owner/name", 9, HEAD).is_err());
    assert!(migration_for(&row.replace("GO owner", "GO other"), "owner/name", 9, HEAD).is_err());
}
