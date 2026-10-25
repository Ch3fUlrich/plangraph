"""KEEPER-PG-RECONNECT S1: a lost Postgres session never answers silently.

Covers I1 (every request gets one status line), I3 (loss is sticky inside the
request), I4 (no half-applied write), I8 (healthy-session errors unchanged),
I10 (one fixed stderr line, never driver text) and I11 (health flags the loss).
Unit tests drive ``PgConnection`` through a fake raw; the two live tests need a
server (``PG_DSN``, provided by the CI pytest-postgres job) and import psycopg
only inside the test body.  Kills always target one backend by pid: CI runs
this suite with ``-n auto``.
"""
import http.client
import os
import time

import pytest

from door_util import Doors
from keeper_util import World, seed
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


def _qmark(sql):
    """Undo ``db_pg.translate`` for the delegate (this path has no literal %)."""
    return sql.replace("%%", "\x00").replace("%s", "?").replace("\x00", "%")


class BridgeCur:
    """Cursor half of :class:`BridgeRaw`: runs on the delegate, raw rows back."""

    def __init__(self, delegate):
        self._delegate = delegate
        self._rows, self.rowcount = [], 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        cur = self._delegate.execute(_qmark(sql), params or ())
        self._rows = list(cur.fetchall())
        self.rowcount = getattr(cur, "rowcount", 0)
        return self

    @property
    def description(self):
        if not self._rows:                      # empty SELECT reads as no result
            return None
        return [type("Col", (), {"name": name})() for name in self._rows[0].keys()]

    def fetchall(self):
        return self._rows


class BridgeRaw:
    """A psycopg-shaped raw that serves every statement from *delegate* ...

    ... except COMMIT, where the session breaks (``broken`` plus a driver
    error) so ``PgConnection`` classifies the loss as ``phase="commit"`` while
    every read and write around it ran for real.
    """

    def __init__(self, delegate):
        self._delegate = delegate
        self.broken = False
        self.closed = False

    def execute(self, sql, params=None):
        control = db_pg.translate_control(sql)
        if control == "commit":
            self.broken = True                  # the session dies at the COMMIT ...
            raise FakeDbError("connection lost")  # ... so the outcome is unknown
        if control == "begin":
            self._delegate.execute("BEGIN IMMEDIATE", ())
            return
        if control is not None:
            self._delegate.execute(sql, ())
            return
        if "pg_advisory" in sql:                # pg-only lock; BEGIN IMMEDIATE locks
            return
        self._delegate.execute(_qmark(sql), params or ())

    def cursor(self):
        return BridgeCur(self._delegate)

    def close(self):
        self.closed = True
        self._delegate.close()

    def fileno(self):
        return -1


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
        conn.commit()                               # lost BEFORE this COMMIT: ...
    assert exc.value.phase == "statement" and raw.calls == calls   # ... nothing was committed (F3)


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


def test_s1_escape_after_status_is_one_line_no_second_status(tmp_path, monkeypatch, capsys):
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    sent = []
    real_send_response = api.KeeperHandler.send_response

    def count_status(self, code, *args, **kwargs):
        sent.append(code)                           # one entry per status line
        return real_send_response(self, code, *args, **kwargs)

    def send_then_boom(self, method):               # noqa: ARG001
        self._json(200, {"status": "ok"})           # the status line is out ...
        raise RuntimeError("after the status line")  # ... then it escapes _handle

    try:
        monkeypatch.setattr(api.KeeperHandler, "send_response", count_status)
        monkeypatch.setattr(api.KeeperHandler, "_handle", send_then_boom)
        status, body = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert (status, body) == (200, {"status": "ok"})
    assert sent == [200]                            # I1: never a second status line
    err = capsys.readouterr().err
    assert [ln for ln in err.splitlines() if ln.startswith("keeper:")] \
        == ["keeper: request failed: RuntimeError"]  # V4: the line comes even after _sent


def test_s1_escape_after_log_is_one_line_total(tmp_path, monkeypatch, capsys):
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=ConnectionLost("statement"))
    doors = Doors(w, tmp_path)
    real_json = api.KeeperHandler._json

    def json_boom(self, status, payload):
        if status == 503:
            raise RuntimeError("answer failed")     # the 503 answer itself fails
        return real_json(self, status, payload)

    try:
        monkeypatch.setattr(api.KeeperHandler, "_json", json_boom)
        status, _ = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 500                            # the last-resort answer still goes out
    err = capsys.readouterr().err
    assert [ln for ln in err.splitlines() if ln.startswith("keeper:")] \
        == ["keeper: request failed: ConnectionLost"]   # the escape never logs a 2nd line


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


def test_s1_lost_before_commit_unavailable_commit_loss_unknown(tmp_path):
    raw = FakeRaw(status=2)                         # inside a transaction
    conn = db_pg.PgConnection(raw, "k_x")
    raw.fail, raw.broken = FakeDbError("gone"), True
    with pytest.raises(ConnectionLost):
        conn.execute("INSERT INTO audit_log(ts) VALUES(?)", ("t",))
    with pytest.raises(ConnectionLost) as exc:
        conn.execute("COMMIT")                      # lost BEFORE the COMMIT ...
    assert exc.value.phase == "statement"           # ... nothing was committed (F3)
    raw2 = FakeRaw(status=2)
    conn2 = db_pg.PgConnection(raw2, "k_x")
    raw2.fail, raw2.broken = FakeDbError("gone"), True
    with pytest.raises(ConnectionLost) as exc2:
        conn2.commit()                              # the COMMIT statement itself loses it
    assert exc2.value.phase == "commit"             # only then is the outcome unknown
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=exc.value)
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("reader", "GET", "/v1/whoami")
        w.store.conn.fail = exc2.value
        status2, body2 = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert (status, body) == (503, {"error": "unavailable",
                                    "msg": "keeper database unavailable"})
    assert (status2, body2) == (
        503, {"error": "unavailable",
              "msg": "keeper database lost at commit; outcome unknown"})


# ------------------------------------------- keep-alive + status-line contract
def test_s1_keepalive_second_escape_still_gets_one_500(tmp_path, monkeypatch):
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    sent, calls = [], []
    real_send, real_handle = (api.KeeperHandler.send_response,
                              api.KeeperHandler._handle)

    def count_status(self, code, *args, **kwargs):
        sent.append(code)                           # one entry per status line
        return real_send(self, code, *args, **kwargs)

    def first_ok_then_boom(self, method):           # noqa: ARG001
        calls.append(method)
        if len(calls) > 1:
            raise RuntimeError("second request escapes")
        return real_handle(self, method)

    conn = http.client.HTTPConnection(              # timeout: red must not hang
        "127.0.0.1", doors.server.server_address[1], timeout=5)
    try:
        monkeypatch.setattr(api.KeeperHandler, "send_response", count_status)
        monkeypatch.setattr(api.KeeperHandler, "_handle", first_ok_then_boom)
        authz = {"Authorization": f"Bearer {w.tok('reader')}"}
        conn.request("GET", "/v1/whoami", headers=authz)
        first = conn.getresponse()
        first.read()
        conn.request("GET", "/v1/whoami", headers=authz)
        second = conn.getresponse()
        body = second.read()
    finally:
        conn.close()
        doors.close()
        w.close()
    assert (first.status, second.status) == (200, 500)
    assert sent == [200, 500]          # I1 keep-alive: one 500 for the escape
    assert b"internal server error" in body


def test_s1_sent_set_only_after_status_line():
    """A failing ``send_response`` must not mark the request as answered."""
    h = api.KeeperHandler.__new__(api.KeeperHandler)

    def boom(status):
        raise OSError("send_response failed")

    h.send_response = boom
    with pytest.raises(OSError):
        h._send(200, b"{}", "application/json")
    assert getattr(h, "_sent", False) is False
    with pytest.raises(OSError):
        h._empty(204)
    assert getattr(h, "_sent", False) is False


def test_s1_commit_break_is_503_outcome_unknown(tmp_path):
    """Fake raw broken only at COMMIT: the write answers 503 outcome unknown."""
    w = World(tmp_path)
    keys = seed(w, [("A", ())])
    w.store.conn = db_pg.PgConnection(BridgeRaw(w.conn), "k_x")
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("router", "POST", "/v1/set_priority",
                                  {"key": keys[0], "n": 5})
    finally:
        doors.close()
        w.close()
    assert status == 503
    assert body == {"error": "unavailable",
                    "msg": "keeper database lost at commit; outcome unknown"}


# ------------------------------------------------------- live Postgres (CI job)
def _admin(psycopg):
    """A separate admin session: kills are addressed by pid, never all backends."""
    return psycopg.connect(DSN, autocommit=True)


def _audit_count(admin, schema):
    """``audit_log`` rows through a session the store does not use."""
    admin.execute(f'SET search_path TO "{schema}"')
    return admin.execute("SELECT count(*) FROM audit_log").fetchone()[0]


def _wait_gone(admin, pid):
    """Poll ``pg_stat_activity`` (own admin session) until backend *pid* is gone.

    ``pg_terminate_backend`` answers before the backend has necessarily
    vanished; without this wait the next statement can race the kill and the
    test flakes.  50 ms steps, 5 s cap.
    """
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        row = admin.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s",
                            (pid,)).fetchone()
        if row[0] == 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"backend {pid} still in pg_stat_activity after 5s")


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
            _wait_gone(admin, pid)                  # the kill must really have landed
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
        pid = conn._raw.info.backend_pid
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(insert, ("2026-01-01T00:00:00Z", "tester", None,
                              "s1", None, 0, None, None))
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        _wait_gone(admin, pid)                      # the kill must really have landed
        with pytest.raises(ConnectionLost):
            conn.execute(insert, ("2026-01-01T00:00:01Z", "tester", None,
                                  "s1b", None, 0, None, None))
        conn.execute("ROLLBACK")                    # no-op on the lost session
        with pytest.raises(ConnectionLost) as exc:
            conn.commit()                           # lost BEFORE this COMMIT: ...
        assert exc.value.phase == "statement"       # ... nothing was committed (F3)
        assert _audit_count(admin, conn.schema) == before   # I4: zero rows of the txn
    finally:
        admin.close()
        w.close()
