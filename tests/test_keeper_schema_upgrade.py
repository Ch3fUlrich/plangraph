"""A schema-v9 SQLite keeper database (no design-3 tables) upgrades when it is opened."""
import sqlite3

import pytest

from keeper_util import PRINCIPALS, Clock, defn
from plangraph.keeper import auth, db as kdb, events as keeper_events, schema
from plangraph.keeper.store import Store

NEW = ("cards", "attempts", "decisions", "questions", "answers", "budget")


def make_v9(path):
    """Build a database exactly as schema v9 installed it: today's DDL minus the design-3 part."""
    assert schema.DESIGN3_DDL in schema.SCHEMA
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.executescript(schema.SCHEMA.replace(schema.DESIGN3_DDL, ""))
    raw.close()


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v9_database_gains_the_design3_tables_at_open(tmp_path):
    path = tmp_path / "keeper.db"
    make_v9(path)
    raw = sqlite3.connect(str(path))
    assert not set(NEW) & tables(raw)
    raw.close()
    conn = kdb.open_db(path)                       # create=False: the upgrade path
    assert set(NEW) <= tables(conn)
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in ("attempts", "decisions", "answers"):
        assert {f"trg_{table}_append_only_update", f"trg_{table}_append_only_delete"} <= triggers
    conn.close()
    kdb.open_db(path).close()                      # idempotent


def test_upgraded_database_keeps_its_chain_and_works(tmp_path):
    path = tmp_path / "keeper.db"
    old = kdb.open_db(path, create=True)
    clock = Clock()
    tokens = {n: auth.add_principal(old, n, role, projects, family=fam)
              for n, role, projects, fam in PRINCIPALS}
    router = auth.authenticate(old, tokens["router"])
    store = Store(old, clock=clock)
    store.create_items(router, [defn("A", [], kind="lane"), defn("GOAL", ["A"], kind="goal")])
    head = keeper_events.read_events(old)[-1]
    old.close()
    # strip the design-3 part to simulate the v9 file, then reopen with current code
    raw = sqlite3.connect(str(path), isolation_level=None)
    for table in ("answers", "questions", "attempts", "decisions", "cards", "budget"):
        raw.execute(f"DROP TABLE {table}")
    raw.close()
    conn = kdb.open_db(path)
    assert set(NEW) <= tables(conn)
    assert keeper_events.read_events(conn)[-1]["hash"] == head["hash"]
    store = Store(conn, clock=clock)
    router = auth.authenticate(conn, tokens["router"])
    store.put_card(router, "demo:A", {"outcome": "o", "window_class": "1m"})
    store.set_state(router, "demo:A", "parked", "upgrade check")
    assert keeper_events.verify_chain(conn) == []
    assert keeper_events.verify_integrity(conn) == []
    conn.close()


def test_schema_version_is_thirteen():
    assert schema.SCHEMA_VERSION == 13


def test_open_without_create_still_refuses_a_non_keeper_file(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    with pytest.raises(RuntimeError):
        kdb.open_db(path)
