"""A schema-v10 SQLite keeper database (metrics table absent) upgrades on open."""
import sqlite3

from keeper_util import Clock
from plangraph.keeper import auth, db as kdb, schema
from plangraph.keeper.store import Store


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def make_v10(path):
    """Build a database exactly as schema v10 installed it: today's DDL minus the metrics table."""
    assert schema.METRICS_DDL in schema.SCHEMA
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.executescript(schema.SCHEMA.replace(schema.METRICS_DDL, ""))
    raw.close()


def test_v10_database_gains_the_metrics_table_at_open(tmp_path):
    path = tmp_path / "keeper.db"
    make_v10(path)
    raw = sqlite3.connect(str(path))
    assert "metrics" not in tables(raw)
    raw.close()
    conn = kdb.open_db(path)                       # create=False: the upgrade path
    assert "metrics" in tables(conn)
    conn.close()
    kdb.open_db(path).close()                       # idempotent


def test_upgraded_database_can_set_and_get_a_metric(tmp_path):
    path = tmp_path / "keeper.db"
    make_v10(path)
    conn = kdb.open_db(path)
    token = auth.add_principal(conn, "router", "router", ["demo"])
    router = auth.authenticate(conn, token)
    store = Store(conn, clock=Clock())
    store.set_metrics(router, "2026-10-16", [{"metric": "latency", "value": 3.5, "source": "upgraded"}])
    rows = store.get_metrics(router, day="2026-10-16", metric="latency")["rows"]
    assert len(rows) == 1 and rows[0]["value"] == 3.5 and rows[0]["source"] == "upgraded"
    conn.close()
