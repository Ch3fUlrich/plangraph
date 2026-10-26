"""Postgres backend units that need no server: translation, rows, selection, schema parity."""
import re
import sqlite3
from pathlib import Path

import pytest

from plangraph.keeper import config as config_mod, db as kdb, db_pg, schema, schema_pg
from plangraph.projector import ddl as projector_ddl


# ----------------------------------------------------------------- translation
def test_placeholders_become_percent_s():
    assert db_pg.translate("SELECT 1 FROM t WHERE a=? AND b=?") == \
        "SELECT 1 FROM t WHERE a=%s AND b=%s"


def test_question_mark_inside_a_literal_is_kept():
    sql = "INSERT INTO t(a,b) VALUES(?, 'why?')"
    assert db_pg.translate(sql) == "INSERT INTO t(a,b) VALUES(%s, 'why?')"


def test_escaped_quote_inside_a_literal():
    assert db_pg.translate("SELECT 'it''s ?', ?") == "SELECT 'it''s ?', %s"


def test_literal_percent_is_escaped_when_params_follow():
    assert db_pg.translate("SELECT '100%' WHERE a=?") == "SELECT '100%%' WHERE a=%s"


def test_no_placeholders_leaves_percent_alone():
    assert db_pg.translate("SELECT '100%'", has_params=False) == "SELECT '100%'"


def test_begin_immediate_is_a_begin_plus_lock():
    assert db_pg.translate_control("BEGIN IMMEDIATE") == "begin"
    assert db_pg.translate_control("  commit ") == "commit"
    assert db_pg.translate_control("ROLLBACK") == "rollback"
    assert db_pg.translate_control("SELECT 1") is None


def test_bool_params_become_ints():
    assert db_pg.adapt_params((True, False, 3, "x", None)) == (1, 0, 3, "x", None)


# ------------------------------------------------------------------------ rows
def test_pgrow_behaves_like_sqlite_row():
    row = db_pg.PgRow(("a", "b"), (1, "two"))
    assert row[0] == 1 and row["b"] == "two" and row[-1] == "two"
    assert row.keys() == ["a", "b"] and list(row) == [1, "two"] and len(row) == 2
    assert dict(row) == {"a": 1, "b": "two"}
    a, b = row
    assert (a, b) == (1, "two")
    with pytest.raises(IndexError):
        row[2]
    with pytest.raises(KeyError):
        row["zzz"]


def test_pgrow_equality_with_tuples_and_rows():
    assert db_pg.PgRow(("a",), (1,)) == db_pg.PgRow(("a",), (1,))
    assert db_pg.PgRow(("a",), (1,)) != db_pg.PgRow(("a",), (2,))


# ------------------------------------------------------------------- DDL guard
@pytest.mark.parametrize("sql", [
    "CREATE TABLE x(a)", "  drop table items", "ALTER TABLE items ADD c TEXT",
    "TRUNCATE events", "GRANT ALL ON items TO PUBLIC", "COPY items FROM STDIN",
    "SET search_path TO evil", "RESET ALL", "DROP TRIGGER trg_events_append_only_update ON events",
    "CREATE OR REPLACE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql",
])
def test_ddl_guard_refuses(sql):
    with pytest.raises(db_pg.DdlDenied):
        db_pg.guard(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM items", "INSERT INTO events(a) VALUES(?)",
    "UPDATE items SET state='open'", "DELETE FROM edges WHERE src=?", "BEGIN IMMEDIATE",
    "SELECT 'create table'",
])
def test_ddl_guard_allows_dml(sql):
    db_pg.guard(sql)


# -------------------------------------------------------------- name hygiene
def test_schema_name_is_derived_from_the_path_and_stable(tmp_path):
    one = db_pg.derive_schema(tmp_path / "a" / "keeper.db")
    assert one == db_pg.derive_schema(tmp_path / "a" / "keeper.db")
    assert one != db_pg.derive_schema(tmp_path / "b" / "keeper.db")
    assert re.fullmatch(r"k_[0-9a-f]{20}", one)


@pytest.mark.parametrize("bad", ["", "a b", "x;drop", "Robert\"", "1abc", "a" * 64, "pg_x"])
def test_schema_name_validation_refuses(bad):
    with pytest.raises(ValueError):
        db_pg.check_schema(bad)


def test_schema_name_validation_accepts():
    assert db_pg.check_schema("keeper_2") == "keeper_2"


def test_dsn_is_redacted_in_errors():
    text = db_pg.redact("could not connect: postgresql://u:s3cret@host/db failed")
    assert "s3cret" not in text and "host" not in text


# ---------------------------------------------------------------- selection
def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("PG_KEEPER_BACKEND", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://127.0.0.1/x")   # a DSN alone selects nothing
    assert kdb.select_backend() == "sqlite"


def test_env_selects_postgres(monkeypatch):
    monkeypatch.setenv("PG_KEEPER_BACKEND", "postgres")
    assert kdb.select_backend() == "postgres"
    assert kdb.select_backend("sqlite") == "sqlite"          # explicit wins over env


def test_unknown_backend_is_refused():
    with pytest.raises(ValueError):
        kdb.select_backend("mysql")


def test_config_carries_backend_options(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_KEEPER_BACKEND", raising=False)
    monkeypatch.delenv("PG_DSN", raising=False)
    cfg = config_mod.Config({}, tmp_path)
    assert cfg.db_options == {}
    cfg = config_mod.Config({"backend": "postgres", "dsn": "postgresql://127.0.0.1/x",
                             "pg_schema": "keeper"}, tmp_path)
    assert cfg.db_options == {"backend": "postgres", "dsn": "postgresql://127.0.0.1/x",
                              "schema": "keeper"}
    assert "127.0.0.1" not in repr(cfg)                      # values never in repr


def test_config_rejects_a_non_string_dsn(tmp_path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.Config({"backend": "postgres", "dsn": 5}, tmp_path)


# ------------------------------------------------------ schema parity (no server)
def _sqlite_tables():
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema.SCHEMA + projector_ddl.SQLITE)
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                " AND name NOT LIKE 'sqlite_%'"):
        out[name] = [(r[1], r[3]) for r in conn.execute(f"PRAGMA table_info({name})")]
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'"
                                      " AND name NOT LIKE 'sqlite_%'")}
    trg = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return out, idx, trg


def _all_pg_ddl():
    return "\n".join(m.sql for m in schema_pg.MIGRATIONS)


# sq12 finding 4: the ONLY order-insensitive columns.  PG migration 7 appends
# them with ALTER TABLE, while fresh SQLite declares them inside the items DDL
# before updated_at, so their position is not comparable across the two.  Every
# other column is compared position for position -- a set-based check passes a
# reorder, which is exactly the bug this replaces.
ORDER_INSENSITIVE = ("exec_workflow_id", "exec_run_id", "exec_attempt",
                     "exec_principal", "exec_retired_runs")


def assert_same_columns(name, cols, pg_cols):
    """Column parity for one table: order for order, nullability per column.

    Only the named ``ORDER_INSENSITIVE`` columns may sit anywhere in the list.
    """
    order = [c for c, _ in cols if c not in ORDER_INSENSITIVE]
    pg_order = [c for c, _ in pg_cols if c not in ORDER_INSENSITIVE]
    assert order == pg_order, f"{name}: column order diverges"
    assert ({c for c, _ in cols if c in ORDER_INSENSITIVE}
            == {c for c, _ in pg_cols if c in ORDER_INSENSITIVE}), \
        f"{name}: order-insensitive columns diverge"
    assert dict(cols) == dict(pg_cols), f"{name}: nullability diverges"


def test_pg_ddl_has_the_same_tables_columns_indexes_and_triggers():
    tables, indexes, triggers = _sqlite_tables()
    ddl = _all_pg_ddl()
    pg_tables = schema_pg.parse_tables(ddl)
    for name, cols in tables.items():
        assert name in pg_tables, name
        assert_same_columns(name, cols, pg_tables[name])
    assert set(pg_tables) == set(tables)
    for name in indexes:
        assert re.search(rf"\b{name}\b", ddl), f"missing index {name}"
    for name in triggers:
        assert re.search(rf"CREATE TRIGGER {name}\b", ddl), f"missing trigger {name}"


def test_parity_check_rejects_a_column_reorder():
    """sq12 finding 4: a set-based column check passes a reorder; this must not."""
    tables, _, _ = _sqlite_tables()
    cols = tables["items"]
    swapped = [cols[1], cols[0]] + cols[2:]
    with pytest.raises(AssertionError):
        assert_same_columns("items", swapped, cols)


def test_parity_check_lets_the_named_order_insensitive_columns_float():
    """The exec_* columns are appended by PG migration 7 (ALTER TABLE ADD
    COLUMN) but declared inside SQLite's items DDL, so only those named columns
    may change position -- and nothing else."""
    tables, _, _ = _sqlite_tables()
    cols = tables["items"]
    floated = ([c for c in cols if c[0] not in ORDER_INSENSITIVE]
               + [c for c in cols if c[0] in ORDER_INSENSITIVE])
    assert_same_columns("items", floated, cols)


def test_migrations_are_versioned_contiguously():
    versions = [m.version for m in schema_pg.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert schema_pg.LATEST == versions[-1]


def test_exec_columns_are_only_added_by_migration_7():
    # sq5: the owner principal (fix 1) and the retired-run fence (fix 2) land in
    # the SAME Postgres migration 7 as the exec triple -- never in a shipped one.
    columns = {"exec_workflow_id", "exec_run_id", "exec_attempt",
               "exec_principal", "exec_retired_runs"}
    for migration in schema_pg.MIGRATIONS[:6]:
        assert not any(column in migration.sql for column in columns)
    migration = schema_pg.MIGRATIONS[6]
    assert migration.version == 7
    for column in columns:
        assert f"ALTER TABLE items ADD COLUMN IF NOT EXISTS {column} " in migration.sql


def test_changelog_migration_numbering_is_postgres_7():
    """V3-1 (haiku): the notes operators migrate from must name Postgres migration 7.

    The exec columns live in migration 7 (migration 6 shipped the JEV shadow), so
    no line may send an operator at migration 6 for them, and the new owner
    principal + retired-run fence columns need their own "Migration 7" section.
    """
    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
        encoding="utf-8")
    for line in text.splitlines():
        lowered = line.lower()
        if "migration 6" in lowered:
            assert not ("exec" in lowered and "migration 7" not in lowered), line
    assert "Migration 7" in text, "CHANGELOG needs a Migration 7 section"
    assert "exec_principal" in text and "exec_retired_runs" in text


def test_design3_tables_are_in_both_schemas():
    tables, _, triggers = _sqlite_tables()
    new = {"cards", "attempts", "decisions", "questions", "answers", "budget"}
    assert new <= set(tables)
    assert new <= set(schema_pg.parse_tables(schema_pg.MIGRATIONS[1].sql))
    assert not new & set(schema_pg.parse_tables(schema_pg.MIGRATIONS[0].sql))
    for table in ("attempts", "decisions", "answers"):
        for verb in ("update", "delete"):
            assert f"trg_{table}_append_only_{verb}" in triggers


def test_metrics_table_is_in_both_schemas():
    tables, _, _ = _sqlite_tables()
    assert "metrics" in tables
    mig = schema_pg.MIGRATIONS[3]
    assert mig.version == 4
    assert "metrics" in schema_pg.parse_tables(mig.sql)
    for earlier in schema_pg.MIGRATIONS[:3]:
        assert "metrics" not in schema_pg.parse_tables(earlier.sql)


def test_jev_shadow_table_is_in_both_schemas():
    tables, _, _ = _sqlite_tables()
    assert "jev_shadow" in tables
    mig = schema_pg.MIGRATIONS[5]
    assert mig.version == 6
    assert "jev_shadow" in schema_pg.parse_tables(mig.sql)
    for earlier in schema_pg.MIGRATIONS[:5]:
        assert "jev_shadow" not in schema_pg.parse_tables(earlier.sql)
    for value in schema.JEV_KINDS + schema.JEV_STATUSES:
        assert f"'{value}'" in mig.sql and f"'{value}'" in schema.JEV_DDL


def test_pg_exit_and_window_checks_match_sqlite():
    sql = schema_pg.MIGRATIONS[1].sql
    for value in schema.EXIT_CLASSES + schema.WINDOW_CLASSES:
        assert f"'{value}'" in sql and f"'{value}'" in schema.DESIGN3_DDL


def test_pg_text_columns_use_binary_collation():
    ddl = _all_pg_ddl()
    for name, body in schema_pg.table_bodies(ddl).items():
        for line in body.splitlines():
            if re.match(r"\s+\w+\s+TEXT\b", line):
                assert 'COLLATE "C"' in line, (name, line)


# ------------------------------------------------- portable-SQL guards (no server)
def _store_sources():
    from pathlib import Path
    root = Path(config_mod.__file__).parent
    names = [p for p in root.glob("*.py")
             if p.name not in ("db.py", "db_pg.py", "db_errors.py", "schema.py", "schema_pg.py",
                               "importer.py", "copy_to_pg.py")]   # copy_to_pg reads a SQLite file
    return {p.name: p.read_text() for p in names}


@pytest.mark.parametrize("needle", ["lastrowid", "INSERT OR ", "except sqlite3.", "sqlite3.Row"])
def test_store_sql_is_portable(needle):
    hits = [n for n, text in _store_sources().items() if needle in text]
    assert hits == [], f"{needle!r} is SQLite-only; use the db_errors tuples / portable SQL: {hits}"


def test_admin_migrate_on_sqlite(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.delenv("PG_KEEPER_BACKEND", raising=False)
    from plangraph.keeper import cli
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(tmp_path / "k.db")}))
    cfg.chmod(0o600)
    assert cli.main(["--config", str(cfg), "admin", "init"]) == 0
    assert cli.main(["--config", str(cfg), "admin", "migrate"]) == 0
    assert "schema up to date" in capsys.readouterr().out


class _FakeRaw:
    """Just enough of a psycopg connection to drive PgConnection's transaction control."""

    def __init__(self, status):
        self.sent = []
        self.info = type("Info", (), {"transaction_status": status})()

    def execute(self, sql, params=None):
        self.sent.append(sql)


def test_commit_and_rollback_only_when_a_transaction_is_open():
    idle = db_pg.PgConnection(_FakeRaw(0), "k_x")
    idle.commit()
    idle.rollback()
    assert idle._raw.sent == []
    busy = db_pg.PgConnection(_FakeRaw(2), "k_x")
    busy.commit()
    busy.rollback()
    assert busy._raw.sent == ["COMMIT", "ROLLBACK"]
