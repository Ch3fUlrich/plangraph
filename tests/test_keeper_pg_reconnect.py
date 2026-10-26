"""KEEPER-PG-RECONNECT S1: a lost Postgres session never answers silently.

Covers I1 (every request gets one status line), I3 (loss is sticky inside the
request), I4 (no half-applied write), I8 (healthy-session errors unchanged),
I10 (one fixed stderr line, never driver text) and I11 (health flags the loss).
Unit tests drive ``PgConnection`` through a fake raw; the two live tests need a
server (``PG_DSN``, provided by the CI pytest-postgres job) and import psycopg
only inside the test body.  Kills always target one backend by pid: CI runs
this suite with ``-n auto``.
"""
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from door_util import Doors
from keeper_util import World, seed
from plangraph.keeper import api, config, daemon, db as kdb, db_pg
from plangraph.keeper.errors import ConnectionLost

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (CI pytest-postgres provides one)")

SRC = Path(__file__).resolve().parents[1] / "src"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            conn.request("GET", "/v1/health")
            resp = conn.getresponse()
            if resp.status == 200:
                conn.close()
                return
            conn.close()
        except Exception:
            time.sleep(0.05)
    raise AssertionError("daemon did not become healthy")


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
    req_lines = [ln for ln in err.splitlines() if ln.startswith("keeper: request failed:")]
    assert req_lines == ["keeper: request failed: ConnectionLost"]   # I10: one fixed line per request
    fatal_lines = [ln for ln in err.splitlines() if ln.startswith("keeper: fatal:")]
    assert fatal_lines == ["keeper: fatal: postgres connection lost"] # S2 fatal line
    assert "host=" not in err                                        # never driver text


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
    assert [ln for ln in err.splitlines() if ln.startswith("keeper: request failed:")] \
        == ["keeper: request failed: ConnectionLost"]   # the escape never logs a 2nd line
    assert [ln for ln in err.splitlines() if ln.startswith("keeper: fatal:")] \
        == ["keeper: fatal: postgres connection lost"]  # S2 F2: still fatal, once


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


def test_s1_mid_send_response_failure_no_second_status():
    """A failure mid send_response leaves _headers_buffer with one status line; _escape must not buffer a 500."""
    h = api.KeeperHandler.__new__(api.KeeperHandler)
    h.protocol_version = "HTTP/1.1"
    h.responses = api.KeeperHandler.responses
    h.request_version = "HTTP/1.1"

    def mid_boom(self, code, message=None):
        self.send_response_only(code, message)
        raise OSError("failed mid send_response")

    h.send_response = lambda code, message=None: mid_boom(h, code, message)

    def handle(method):
        h._send(200, b"{}", "application/json")

    h._handle = handle
    h._request("GET")
    assert len(h._headers_buffer) == 1
    assert b"200" in h._headers_buffer[0]


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


# ------------------------------------------------------------- S2 fatal exit
def test_s2_lock_alive_reflects_session_state():
    """PgKeeperLock.alive returns False on None/closed/broken raw or readable descriptor."""
    lock = db_pg.PgKeeperLock(None)
    assert not lock.alive()

    raw = FakeRaw()
    lock = db_pg.PgKeeperLock(raw)
    raw.broken = True
    assert not lock.alive()
    raw.broken = False
    raw.closed = True
    assert not lock.alive()

    s1, s2 = socket.socketpair()
    try:
        raw_sock = FakeRaw()
        raw_sock.fileno = s1.fileno
        sock_lock = db_pg.PgKeeperLock(raw_sock)
        assert sock_lock.alive()

        s2.close()
        assert not sock_lock.alive()
    finally:
        s1.close()


def test_s2_fatal_from_handler_sets_stop(tmp_path):
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=ConnectionLost("statement"))
    doors = Doors(w, tmp_path)
    stopped = threading.Event()
    doors.server.on_fatal = stopped.set
    try:
        status, body = doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert status == 503
    assert body == {"error": "unavailable", "msg": "keeper database unavailable"}
    assert getattr(doors.server, "fatal_reason", None) == "postgres connection lost"
    assert stopped.is_set()


def test_s2_health_503_when_fatal_conn_healthy(tmp_path):
    """I11: fatal_reason alone flips /v1/health to 503 even on a healthy conn (F3)."""
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    try:
        assert doors.http("reader", "GET", "/v1/health") == (200, {"status": "ok"})
        doors.server.fail("postgres keeper lock session lost")
        status, body = doors.http("reader", "GET", "/v1/health")
    finally:
        doors.close()
        w.close()
    assert (status, body) == (503, {"status": "unavailable"})


def test_s2_fail_concurrent_once(tmp_path, capsys):
    """F1: concurrent fail() callers print one fatal line and call on_fatal once."""
    w = World(tmp_path)
    doors = Doors(w, tmp_path)
    fired = []
    doors.server.on_fatal = lambda: fired.append(True)
    n = 8
    barrier = threading.Barrier(n)

    def hit():
        barrier.wait()
        doors.server.fail("postgres keeper lock session lost")

    threads = [threading.Thread(target=hit) for _ in range(n)]
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)         # widen the check-then-set race window
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    finally:
        sys.setswitchinterval(old_interval)
        doors.close()
        w.close()
    assert all(not t.is_alive() for t in threads)
    err = capsys.readouterr().err
    fatal = [ln for ln in err.splitlines() if ln.startswith("keeper: fatal:")]
    assert fatal == ["keeper: fatal: postgres keeper lock session lost"]
    assert len(fired) == 1


def test_s2_fail_after_503_write_raises(tmp_path, monkeypatch):
    """F2: a dead client must not skip the fatal; fail() runs after the 503 write."""
    w = World(tmp_path)
    w.store.conn = ConnProxy(w.store.conn, fail=ConnectionLost("statement"))
    doors = Doors(w, tmp_path)
    real_json = api.KeeperHandler._json

    def write_boom(self, status, payload):
        if status == 503:
            raise BrokenPipeError("client gone")    # the 503 write itself fails
        return real_json(self, status, payload)

    try:
        monkeypatch.setattr(api.KeeperHandler, "_json", write_boom)
        doors.http("reader", "GET", "/v1/whoami")
    finally:
        doors.close()
        w.close()
    assert doors.server.fatal_reason == "postgres connection lost"


def test_s2_dead_lock_session_exits_75(tmp_path, monkeypatch):
    w = World(tmp_path)
    port = free_port()
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(tmp_path / "keeper.db"),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{port}",
    }), encoding="utf-8")
    cfg_path.chmod(0o600)
    cfg = config.load_config(cfg_path)

    class FakeLock:
        def __init__(self):
            self.checks = 0

        def alive(self):
            self.checks += 1
            return self.checks <= 1  # turns False after first tick

        def release(self):
            pass

    fake_lock = FakeLock()
    monkeypatch.setattr(kdb, "lock_keeper", lambda *args, **kwargs: fake_lock)
    monkeypatch.setattr(kdb, "open_db", lambda *args, **kwargs: w.conn)

    timer = threading.Timer(3.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        t0 = time.monotonic()
        rc = daemon.run(cfg)
        duration = time.monotonic() - t0
    finally:
        timer.cancel()

    assert rc == 75
    assert duration < 5.0


def test_s2_shutdown_bounded(tmp_path, monkeypatch):
    w = World(tmp_path)
    port = free_port()
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(tmp_path / "keeper.db"),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{port}",
    }), encoding="utf-8")
    cfg_path.chmod(0o600)
    cfg = config.load_config(cfg_path)

    unblock = threading.Event()
    real_close = api.KeeperHTTPServer.server_close

    def blocking_close(self):
        unblock.wait(timeout=2.0)
        real_close(self)

    monkeypatch.setattr(api.KeeperHTTPServer, "server_close", blocking_close)
    monkeypatch.setattr(kdb, "open_db", lambda *args, **kwargs: w.conn)
    monkeypatch.setattr(daemon, "SHUTDOWN_TIMEOUT", 0.2, raising=False)

    exited = []
    monkeypatch.setattr(os, "_exit", exited.append)

    def trigger_fatal(srv):
        srv.fail("fatal before shutdown")

    timer = threading.Timer(3.5, lambda: (unblock.set(), os.kill(os.getpid(), signal.SIGTERM)))
    timer.start()
    try:
        rc = daemon.run(cfg, ready=trigger_fatal)
    finally:
        unblock.set()
        timer.cancel()

    assert 75 in exited or rc == 75


@needs_pg
def test_s2_daemon_exits_75_within_10s(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    app_name = f"pg_app_{uuid.uuid4().hex[:12]}"
    w = World(tmp_path, backend="postgres", dsn=DSN)
    schema = w.conn.schema
    tok = w.tok("reader")
    w.close()

    port = free_port()
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(tmp_path / "keeper.db"),
        "backend": "postgres",
        "dsn": DSN,
        "pg_schema": schema,
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": f"127.0.0.1:{port}",
    }), encoding="utf-8")
    cfg_path.chmod(0o600)

    env = dict(os.environ)
    env["PGAPPNAME"] = app_name
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "plangraph.keeper.daemon", "--config", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    try:
        wait_health(port)
        admin = _admin(psycopg)
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s",
                (app_name,))
        finally:
            admin.close()

        got = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/v1/whoami", headers={"Authorization": f"Bearer {tok}"})
            resp = conn.getresponse()
            got = resp.status
            conn.close()
        except (ConnectionRefusedError, BrokenPipeError):
            got = "refused"
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                got = "refused"
            else:
                raise
        assert got in (503, "refused")

        assert proc.wait(timeout=10) == 75
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

