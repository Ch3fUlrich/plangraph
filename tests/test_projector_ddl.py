"""Projector tables (keeper migration 3) on SQLite and Postgres, and the projector records."""
import dataclasses
import os
import sqlite3
import uuid

import pytest

from plangraph.keeper import db as kdb, schema_pg
from plangraph.projector import ddl
from plangraph.projector.models import (
    ATTEMPT_ACTIVITY_TYPES, EXIT_CLASSES, SETTABLE_STATES, AttemptRec, RunProjection, StateRec)

TABLES = {"projector_applied", "projector_watermark", "projector_alarms"}
NOW = "2026-10-15T10:00:00Z"


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def open_sqlite(path, **kw):
    return kdb.open_db(path, backend="sqlite", **kw)


def open_upgraded(path):
    """Create the keeper, then re-open it the way every later process does (runs ``_upgrade``)."""
    open_sqlite(path, create=True).close()
    return open_sqlite(path)


def add_alarm(conn, kind="state_drift", workflow_id="card:A", key="k", cleared_at=None):
    conn.execute("INSERT INTO projector_alarms(kind, workflow_id, key, detail, raised_at, cleared_at)"
                 " VALUES(?,?,?,?,?,?)", (kind, workflow_id, key, "d", NOW, cleared_at))


# ---------------------------------------------------------------------- sqlite
def test_fresh_sqlite_keeper_has_the_projector_tables(tmp_path):
    conn = open_upgraded(tmp_path / "keeper.db")
    assert TABLES <= tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projector_applied)")}
    assert cols == {"workflow_id", "run_id", "activity_id", "attempt", "kind", "key", "n",
                    "applied_at"}
    conn.close()


def test_old_sqlite_keeper_gains_the_tables_at_open(tmp_path):
    path = tmp_path / "keeper.db"
    open_upgraded(path).close()
    raw = sqlite3.connect(str(path), isolation_level=None)
    for name in TABLES:
        raw.execute(f"DROP TABLE {name}")
    assert not TABLES & tables(raw)
    raw.close()
    conn = open_sqlite(path)
    assert TABLES <= tables(conn)
    conn.close()
    conn = open_sqlite(path)                      # idempotent
    assert TABLES <= tables(conn)
    conn.close()


def test_applied_kind_is_checked_and_key_is_unique(tmp_path):
    conn = open_upgraded(tmp_path / "keeper.db")
    row = ("wf", "r1", "1", 1, "attempt", "p:A", 1, NOW)
    sql = ("INSERT INTO projector_applied(workflow_id, run_id, activity_id, attempt, kind, key,"
           " n, applied_at) VALUES(?,?,?,?,?,?,?,?)")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
    conn.execute(sql, row[:4] + ("state",) + row[5:])        # same ids, other kind
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row[:4] + ("bogus",) + row[5:])
    conn.close()


def test_alarm_dedupe_index_refuses_a_second_open_alarm(tmp_path):
    conn = open_upgraded(tmp_path / "keeper.db")
    add_alarm(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_alarm(conn)
    add_alarm(conn, key="other")                              # other key
    add_alarm(conn, kind="orphan_running")                    # other kind
    add_alarm(conn, workflow_id="card:B")                     # other workflow
    conn.execute("UPDATE projector_alarms SET cleared_at=? WHERE key='k' AND workflow_id='card:A'"
                 " AND kind='state_drift'", (NOW,))
    add_alarm(conn)                                           # allowed once the first is cleared
    add_alarm(conn, cleared_at=NOW)                           # any number of cleared ones
    conn.close()


# ----------------------------------------------------------------------- models
def _attempt():
    return AttemptRec("wf", "r1", "5", 1, 5, 8, "p:A", "ok", "h", "m", "h/m", "256k",
                      "2026-10-15T10:00:00Z", "2026-10-15T10:01:00Z", 10, 5, 0.125)


def test_models_are_frozen_and_equal_by_value():
    a, s = _attempt(), StateRec("wf", "r1", 3, "p:A", "running", None)
    assert a == _attempt() and s == StateRec("wf", "r1", 3, "p:A", "running", None)
    assert hash(a) == hash(_attempt())
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.exit_class = "timeout"
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.state = "failed"
    run = RunProjection("wf", "r1", "p:A", "completed", NOW, 9, (a,), (s,))
    assert run == RunProjection("wf", "r1", "p:A", "completed", NOW, 9, (a,), (s,))
    assert run != dataclasses.replace(run, status="failed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.status = "failed"
    assert isinstance(run.attempts, tuple) and isinstance(run.states, tuple)


def test_model_field_order_follows_the_spec():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(AttemptRec) == [
        "workflow_id", "run_id", "activity_id", "attempt", "scheduled_event_id",
        "closed_event_id", "plan_key", "exit_class", "harness", "model", "combo", "class_",
        "started_at", "ended_at", "tokens_in", "tokens_out", "cost"]
    assert names(StateRec) == ["workflow_id", "run_id", "event_id", "plan_key", "state", "reason"]
    assert names(RunProjection) == ["workflow_id", "run_id", "plan_key", "status", "start_time",
                                    "last_event_id", "attempts", "states"]


def test_constants_come_from_the_keeper():
    from plangraph.keeper import schema, store_cards
    assert ATTEMPT_ACTIVITY_TYPES == frozenset({"attempt"})
    assert EXIT_CLASSES is schema.EXIT_CLASSES
    assert SETTABLE_STATES is store_cards.SETTABLE_STATES
    assert "done" not in SETTABLE_STATES and "cancelled" not in SETTABLE_STATES


def test_projector_package_root_has_no_side_effects():
    import pathlib
    import subprocess
    import sys
    import plangraph
    src = str(pathlib.Path(plangraph.__file__).resolve().parent.parent)
    code = ("import sys, plangraph.projector; "
            "bad = [m for m in sys.modules if m.split('.')[0] == 'temporalio' "
            "or m.startswith('plangraph.keeper')]; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": src}).returncode == 0


def test_migration_three_is_the_projector_ddl():
    assert ddl.LATEST_PROJECTOR_MIGRATION == 3 == schema_pg.LATEST
    last = schema_pg.MIGRATIONS[-1]
    assert (last.version, last.sql) == (3, ddl.PG)
    assert TABLES == set(schema_pg.parse_tables(ddl.PG))
    assert 'COLLATE "C"' in ddl.PG and "WHERE cleared_at IS NULL" in ddl.PG


# --------------------------------------------------------------------- postgres
PG_DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(
    os.environ.get("PG_KEEPER_BACKEND") != "postgres" or not PG_DSN,
    reason="PG_KEEPER_BACKEND=postgres and PG_DSN needed")


@pytest.fixture
def pg():
    import psycopg
    from plangraph.keeper import db_pg
    name = "t_projector_" + uuid.uuid4().hex[:12]
    raw = psycopg.connect(PG_DSN, autocommit=True, prepare_threshold=None)
    try:
        yield raw, name, db_pg
    finally:
        raw.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        raw.close()


def pg_tables(raw, name):
    return {r[0] for r in raw.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (name,))}


def pg_versions(raw):
    return [r[0] for r in raw.execute("SELECT version FROM schema_migrations ORDER BY version")]


@needs_pg
def test_pg_migrations_apply_to_v3(pg):
    raw, name, db_pg = pg
    assert db_pg.migrate(raw, name) == [1, 2, 3]
    assert TABLES <= pg_tables(raw, name)
    assert pg_versions(raw) == [1, 2, 3]
    assert db_pg.migrate(raw, name) == []


@needs_pg
def test_pg_upgrade_from_v2_adds_the_projector_tables(pg, monkeypatch):
    raw, name, db_pg = pg
    full = schema_pg.MIGRATIONS
    monkeypatch.setattr(schema_pg, "MIGRATIONS", full[:2])
    assert db_pg.migrate(raw, name) == [1, 2]
    assert not TABLES & pg_tables(raw, name)
    monkeypatch.setattr(schema_pg, "MIGRATIONS", full)
    assert db_pg.migrate(raw, name) == [3]
    assert TABLES <= pg_tables(raw, name)
    assert pg_versions(raw) == [1, 2, 3]


@needs_pg
def test_pg_alarm_dedupe_and_kind_check(pg):
    import psycopg
    raw, name, db_pg = pg
    db_pg.migrate(raw, name)
    raw.execute(f'SET search_path TO "{name}"')
    ins = ("INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at, cleared_at)"
           " VALUES(%s,'state_drift','card:A','k','d',%s,%s)")
    raw.execute(ins, (1, NOW, None))
    with pytest.raises(psycopg.errors.UniqueViolation):
        raw.execute(ins, (2, NOW, None))
    raw.execute("UPDATE projector_alarms SET cleared_at=%s WHERE id=1", (NOW,))
    raw.execute(ins, (2, NOW, None))
    with pytest.raises(psycopg.errors.CheckViolation):
        raw.execute("INSERT INTO projector_applied VALUES('w','r','a',1,'bogus','k',NULL,%s)", (NOW,))
