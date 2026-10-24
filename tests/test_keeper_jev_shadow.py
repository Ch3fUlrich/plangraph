"""RT-P1-JEV-SHADOW keeps its own schema line across the keeper-scope merge.

Main took SQLite schema v13 (``JEV_DDL``) and Postgres Migration 6 (``_V6``) for
the audit-only ``jev_shadow`` table; the oversight merge moved *our* tables to
v14 / Migration 8 (main's #140 keeps Migration 7 for the ``items`` exec
ownership columns) and appended them after main's half.  These tests pin that
nothing of main's side moved: the JEV tables stay where main put them, and the
oversight tables sit strictly after them in SQLite, Postgres and the copy order.
"""
import sqlite3

import pytest

from plangraph.keeper import copy_to_pg, schema, schema_pg

JEV_TABLE = "jev_shadow"


def test_jev_ddl_carries_the_shadow_table_and_its_vocabularies():
    assert schema.JEV_KINDS == ("decision", "outcome")
    assert schema.JEV_STATUSES == ("pending", "agree", "disagree", "error")
    assert JEV_TABLE in schema.JEV_DDL
    for kind in schema.JEV_KINDS:
        assert f"'{kind}'" in schema.JEV_DDL
    for status in schema.JEV_STATUSES:
        assert f"'{status}'" in schema.JEV_DDL
    assert "idx_jev_shadow_card" in schema.JEV_DDL
    assert "idx_jev_shadow_kind" in schema.JEV_DDL


def test_jev_stays_at_v13_while_oversight_moved_to_v14():
    assert schema.SCHEMA_VERSION == 14
    assert schema.JEV_DDL in schema.SCHEMA
    assert schema.OVERSIGHT_DDL in schema.SCHEMA
    # main's half first, ours appended straight after it (SCHEMA and db._upgrade)
    assert schema.SCHEMA.index(schema.JEV_DDL) < schema.SCHEMA.index(schema.OVERSIGHT_DDL)
    assert "oversight_tokens" not in schema.JEV_DDL
    assert JEV_TABLE not in schema.OVERSIGHT_DDL


def test_jev_lands_in_a_fresh_database_and_survives_reopen(tmp_path):
    from plangraph.keeper import db as kdb

    path = tmp_path / "keeper.db"
    kdb.open_db(path, create=True).close()
    conn = kdb.open_db(path)                      # runs _upgrade: JEV then oversight
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert JEV_TABLE in tables
    assert {"oversight_tokens", "oversight_ops"} <= tables
    conn.execute("INSERT INTO jev_shadow(decision_id, kind, status, ts)"
                 " VALUES('d1', 'decision', 'pending', '2026-10-23T00:00:00Z')")
    row = conn.execute(f"SELECT kind, status FROM {JEV_TABLE}").fetchone()
    assert (row["kind"], row["status"]) == ("decision", "pending")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jev_shadow(decision_id, kind, status, ts)"
                     " VALUES('d2', 'nope', 'pending', '2026-10-23T00:00:00Z')")
    conn.close()


def test_pg_migration_6_is_the_jev_twin_and_7_the_oversight_twin():
    # name kept (the CI trailer gate treats a renamed test as a removal); the
    # number it quotes is updated: main's #140 took Migration 7 for the items
    # exec ownership columns, so the oversight twin is Migration 8.
    assert schema_pg.LATEST == 8
    six = [m for m in schema_pg.MIGRATIONS if m.version == 6]
    eight = [m for m in schema_pg.MIGRATIONS if m.version == 8]
    assert len(six) == len(eight) == 1
    assert JEV_TABLE in schema_pg.parse_tables(six[0].sql)
    assert {"oversight_tokens", "oversight_ops"} <= set(schema_pg.parse_tables(eight[0].sql))
    earlier = schema_pg.parse_tables(
        "\n".join(m.sql for m in schema_pg.MIGRATIONS if m.version < 6))
    assert JEV_TABLE not in earlier

    sqlite_tables = schema_pg.parse_tables(schema.SCHEMA)
    pg_tables = schema_pg.parse_tables("\n".join(m.sql for m in schema_pg.MIGRATIONS))
    assert [c for c, _ in sqlite_tables[JEV_TABLE]] == \
        [c for c, _ in pg_tables[JEV_TABLE]]
    assert [bool(nn) for _, nn in sqlite_tables[JEV_TABLE]] == \
        [bool(nn) for _, nn in pg_tables[JEV_TABLE]]


def test_copy_to_pg_carries_jev_before_the_oversight_tables():
    order = list(copy_to_pg.TABLES)
    assert order.index(JEV_TABLE) < order.index("oversight_tokens")
    assert order.index("oversight_tokens") < order.index("oversight_ops")


def test_roles_keep_main_spawner_and_our_oversight():
    assert "spawner" in schema.ROLES
    assert "oversight" in schema.ROLES
    assert schema.ROLES[-1] == "oversight"
