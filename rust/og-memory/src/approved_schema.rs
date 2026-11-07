//! Approved D-4743 V1 validation capabilities; not live-schema application.
pub(super) const SCHEMA: &str = include_str!("../tests/fixtures/memory-v1-approved.pg");

#[test]
fn approved_schema_matches_validation_fixture() {
    assert_eq!(
        SCHEMA,
        include_str!("../tests/fixtures/memory-v1-approved.pg")
    );
}
