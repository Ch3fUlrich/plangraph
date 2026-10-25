"""KEEPER-PG-RECONNECT S1: a lost Postgres session never answers silently.

Covers I1 (every request gets one status line), I3 (loss is sticky inside the
request), I4 (no half-applied write), I8 (healthy-session errors unchanged),
I10 (one fixed stderr line, never driver text) and I11 (health flags the loss).
Unit tests drive ``PgConnection`` through a fake raw; the two live tests need a
server (``PG_DSN``, provided by the CI pytest-postgres job) and import psycopg
only inside the test body.  Kills always target one backend by pid: CI runs
this suite with ``-n auto``.
"""
import os

import pytest

from door_util import Doors
from keeper_util import World
from plangraph.keeper import api, db_pg
from plangraph.keeper.errors import ConnectionLost

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (CI pytest-postgres provides one)")


# ----------------------------------------------------------- fake driver raw
class FakeCur:
    """Cursor half of :class:`FakeRaw`: delegates execute, fetches nothing."""

    description = None
    rowcount = 0

    def __init__(self, raw):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._raw.execute(sql, params)

    def fetchall(self):
        return []


class FakeRaw:
    """Just enough of a psycopg connection for :class:`db_pg.PgConnection`."""

    def __init__(self, status=0):
        self.calls = []
        self.fail = None          # exception raised by execute ...
        self.fail_on = None       # ... only for SQL containing this (None: every call)
        self.broken = False
        self.closed = False
        self.close_error = None
        self.info = type("Info", (), {"transaction_status": status})()

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if self.fail is not None and (self.fail_on is None or self.fail_on in sql):
            raise self.fail

    def cursor(self):
        return FakeCur(self)

    def fileno(self):
        return -1

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeDbError(Exception):
    """Stand-in for a driver error (e.g. LockNotAvailable on lock_timeout)."""


class ConnProxy:
    """Wraps a world connection so a request fails at the DB seam."""

    def __init__(self, wrapped, fail=None, lost=False):
        self._wrapped = wrapped
        self.fail = fail
        self.lost = lost

    def execute(self, sql, params=()):
        if self.fail is not None:
            raise self.fail
        return self._wrapped.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


# -------------------------------------------------------------- unit (fake raw)
def test_s1_lost_is_sticky_until_boundary():
    raw = FakeRaw(status=2)
    conn = db_pg.PgConnection(raw, "k_x")
    raw.fail, raw.broken = FakeDbError("gone"), True
    with pytest.raises(ConnectionLost) as exc:
        conn.execute("SELECT 1 FROM items")
    assert exc.value.phase == "statement" and conn.lost
    calls = list(raw.calls)
    with pytest.raises(ConnectionLost):
        conn.execute("INSERT INTO audit_log(ts) VALUES(?)", ("t",))
    with pytest.raises(ConnectionLost):
        conn.execute("BEGIN IMMEDIATE")
    assert raw.calls == calls                       # I3: no raw IO again in this request
    assert conn.execute("ROLLBACK").rowcount == 0   # the only tolerated control
    raw.close_error = FakeDbError("close boom")
    conn.close()                                    # closing a dead session never raises
    # the BEGIN-failure cleanup never runs against a dead session
    raw2 = FakeRaw()
    conn2 = db_pg.PgConnection(raw2, "k_x")
    raw2.fail, raw2.fail_on = FakeDbError("gone"), "pg_advisory_xact_lock"
    raw2.broken = True
    with pytest.raises(ConnectionLost):
        conn2.execute("BEGIN IMMEDIATE")
    assert "ROLLBACK" not in raw2.calls


def test_s1_rollback_on_lost_is_noop_commit_raises():
    raw = FakeRaw(status=2)                         # inside a transaction
    conn = db_pg.PgConnection(raw, "k_x")
    raw.fail, raw.broken = FakeDbError("gone"), True
    with pytest.raises(ConnectionLost) as exc:
        conn.execute("COMMIT")
    assert exc.value.phase == "commit"
    assert conn.lost and not conn.in_transaction
    calls = list(raw.calls)
    conn.rollback()                                 # I4: no-op, no IO on the dead session
    assert raw.calls == calls
    with pytest.raises(ConnectionLost) as exc:
        conn.commit()
    assert exc.value.phase == "commit" and raw.calls == calls


def test_s1_lock_timeout_not_lost():
    raw = FakeRaw()
    conn = db_pg.PgConnection(raw, "k_x")
    raw.fail = FakeDbError("lock timeout")          # I8: error, but the session is healthy
    raw.fail_on = "pg_advisory_xact_lock"
    with pytest.raises(FakeDbError) as exc:
        conn.execute("BEGIN IMMEDIATE")
    assert exc.value is raw.fail                    # the original error, unchanged
    assert "ROLLBACK" in raw.calls                  # the failed BEGIN is cleaned up as today
    assert not conn.lost
    raw.fail = None
    conn.execute("SELECT 1 FROM items")             # the session still works


# ---------------------------------------------------------- HTTP (Doors + World)
def test_s1_auth_conn_lost_is_503_json(tmp_path):
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=ConnectionLost("statement"))
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 503                            # one JSON answer, never a silent close
    assert body == {"error": "unavailable", "msg": "keeper database unavailable"}


def test_s1_unexpected_escape_is_500(tmp_path, monkeypatch):
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    try:
        def boom(self, method):                     # noqa: ARG001 - escapes _handle
            raise RuntimeError("escaped the route")
        monkeypatch.setattr(api.KeeperHandler, "_handle", boom)
        status, body = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 500
    assert body == {"error": "internal", "msg": "internal server error"}


def test_s1_one_line_no_dsn(tmp_path, capsys):
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=ConnectionLost("statement"))
    doors = Doors(w, tmp_path)
    try:
        status, _ = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 503
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("keeper:")]
    assert lines == ["keeper: request failed: ConnectionLost"]   # I10: one fixed line
    assert "postgres" not in err and "host=" not in err          # never driver text


def test_s1_health_503_when_lost(tmp_path):
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    try:
        assert doors.http("reader", "GET", "/v1/health") == (200, {"status": "ok"})
        w.store.conn = ConnProxy(w.store.conn, lost=True)   # I11: flag only, no IO
        status, body = doors.http("reader", "GET", "/v1/health")
    finally:
        doors.close()
        w.close()
    assert status == 503 and body == {"status": "unavailable"}


# ------------------------------------------------------- live Postgres (CI job)
def _admin(psycopg):
    """A separate admin session: kills are addressed by pid, never all backends."""
    return psycopg.connect(DSN, autocommit=True)


def _audit_count(admin, schema):
    """``audit_log`` rows through a session the store does not use."""
    admin.execute(f'SET search_path TO "{schema}"')
    return admin.execute("SELECT count(*) FROM audit_log").fetchone()[0]


@needs_pg
def test_s1_pg_kill_next_call_is_503_not_disconnect(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    w = World(tmp_path, backend="postgres", dsn=DSN)
    doors = Doors(w, tmp_path)
    try:
        pid = w.conn._raw.info.backend_pid
        admin = _admin(psycopg)
        try:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            admin.close()
        status, body = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 503, body                      # the incident symptom was a dropped socket
    assert body["error"] == "unavailable"


@needs_pg
def test_s1_mid_txn_kill_commits_nothing(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    w = World(tmp_path, backend="postgres", dsn=DSN)
    conn = w.conn
    insert = ("INSERT INTO audit_log(ts,actor,role,action,key,ok,code,msg)"
              " VALUES(?,?,?,?,?,?,?,?)")
    admin = _admin(psycopg)
    try:
        before = _audit_count(admin, conn.schema)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(insert, ("2026-01-01T00:00:00Z", "tester", None,
                              "s1", None, 0, None, None))
        admin.execute("SELECT pg_terminate_backend(%s)",
                      (conn._raw.info.backend_pid,))
        with pytest.raises(ConnectionLost):
            conn.execute(insert, ("2026-01-01T00:00:01Z", "tester", None,
                                  "s1b", None, 0, None, None))
        conn.execute("ROLLBACK")                    # no-op on the lost session
        with pytest.raises(ConnectionLost) as exc:
            conn.commit()
        assert exc.value.phase == "commit"
        assert _audit_count(admin, conn.schema) == before   # I4: zero rows of the txn
    finally:
        admin.close()
        w.close()
