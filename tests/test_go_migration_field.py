"""One parser of the MIGRATION field of a GO statement, shared by the batch land gate and land-train."""
import pytest

from plangraph.keeper.store_waiver import migration_field, migration_for_go

HEAD = "a1" * 20


@pytest.mark.parametrize("statement, expected", [
    ("GO #9; MIGRATION 3; PLAN x", 3),
    ("GO #9", None),
    ("MIGRATION 3 GO #9", None),
    ("GO #9; MIGRATION-NOTE staged; MIGRATION 3", 3),
    ("GO #9; MIGRATION-NOTE staged", None),
    ("GO #9; MIGRATION", None),
])
def test_the_field_is_read(statement, expected):
    assert migration_field(statement) == expected


@pytest.mark.parametrize("statement", [
    "GO #9; MIGRATION +3", "GO #9; MIGRATION 03", "GO #9; MIGRATION  3", "GO #9; MIGRATION 0",
    "GO #9; MIGRATION three", "GO #9; MIGRATION 3; MIGRATION 3",
])
def test_a_malformed_or_repeated_field_is_refused(statement):
    with pytest.raises(ValueError):
        migration_field(statement)


def test_a_row_for_another_pull_request_or_head_supplies_no_migration():
    row = f"GO owner/name PR #9 GO-HEAD {HEAD}; MIGRATION 3"
    assert migration_for_go(row, "owner/name", 9, HEAD) == 3
    assert migration_for_go(f"GO #9 GO-HEAD {HEAD}", "owner/name", 9, HEAD) is None
    assert migration_for_go(f"(15:46Z, date -u) {row}", "owner/name", 9, HEAD.upper()) == 3
    for args in (("owner/name", 9, "b" * 40), ("owner/name", 10, HEAD)):
        with pytest.raises(ValueError, match="not #"):
            migration_for_go(row, *args)
    for bad in ("GO #9; MIGRATION 3", f"GO #9 GO-HEAD {HEAD} GO-HEAD {HEAD}; MIGRATION 3", "NO GO"):
        with pytest.raises(ValueError):
            migration_for_go(bad, "owner/name", 9, HEAD)
    with pytest.raises(ValueError):
        migration_for_go(row.replace("GO owner", "GO other"), "owner/name", 9, HEAD)
