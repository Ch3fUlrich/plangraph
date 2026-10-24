"""KEEPER-PG-RECONNECT: a lost Postgres session reconnects once, or the keeper exits non-zero.

Four vectors, red-first (each was written against the pre-fix keeper and failed
before the fix landed):

* **V1** -- ``PgConnection.execute`` reconnects and re-runs exactly once, and
  only for a statement that cannot double-apply (an idempotent read or a
  pre-write ``BEGIN``).  An open write transaction, its ``COMMIT`` and an
  autocommit write come back as ``ConnectionLost`` and are answered with
  ``503 {"error": "unavailable"}`` -- the statement never runs a second time.
* **V2** -- ``RECONNECT_ATTEMPTS`` reconnect failures produce exactly one
  redacted log line and the daemon exits non-zero.
* **V3** -- every handler exception is answered with an HTTP response (500/503
  JSON) plus one log line redacted through ``db_pg.redact``; no request is ever
  left without a response.
* **V4** -- a lost ``PgKeeperLock`` connection is fatal: the daemon exits
  non-zero instead of serving as the singleton writer it no longer is.

The ``PG_DSN`` gate test kills the keeper's own backend with
``pg_terminate_backend`` and requires the next authed call to answer 200
within 10 s (CI ``pytest-postgres`` job; skipped where no server is set).
psycopg-dependent units skip where psycopg is not installed (the default CI
job installs psycopg only in the Postgres service job).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from door_util import Doors
from keeper_util import World, seed
from plangraph.keeper import daemon, db as kdb, db_pg

#: shaped like the real thing (never a live one) so redaction can be asserted.
FAKE_DSN = "postgresql://u:s3cret@keeper-db/plangraph"

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")


# ------------------------------------------------------------------- doubles
class _Info:
    def __init__(self, status=0):
        self.transaction_status = status


class Raw:
    """A psycopg connection double: ``fail(sql)`` returns the error that loses the session."""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.info = _Info()
        self.closed = False

    def _maybe_raise(self, sql):
        if self.fail is not None:
            exc = self.fail(sql)
            if exc is not None:
                raise exc

    def execute(self, sql, params=None):
        self.calls.append(sql)
        self._maybe_raise(sql)
        return self

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rowcount = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.raw.calls.append(sql)
        self.raw._maybe_raise(sql)
        self.description = None
        self._rows = []
        self.rowcount = 0
        if sql.lstrip().lower().startswith("select"):
            self.description = [type("Col", (), {"name": "n"})()]
            self._rows = [(1,)]

    def fetchall(self):
        return self._rows


def _pg_conn(raw, monkeypatch, fresh=None):
    """A ``PgConnection`` over *raw* whose next session is *fresh* (one reconnect)."""
    made = []
    if fresh is not None:
        def _connect(dsn, busy_ms):
            made.append(dsn)
            return fresh
        monkeypatch.setattr(db_pg, "_connect", _connect)
    conn = db_pg.PgConnection(raw, "k_x", dsn=FAKE_DSN, busy_ms=50)
    return conn, made


def _lost(psycopg):
    """The error a dropped socket raises: an OperationalError with no alive SQLSTATE."""
    return psycopg.OperationalError("server closed the connection unexpectedly")


def _keeper_lines(err):
    return [ln for ln in err.splitlines() if ln.startswith("keeper:")]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


# -------------------------------------------------------------------------- V1
def test_v1_read_is_reconnected_and_retried_exactly_once(monkeypatch, capsys):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    first = Raw(fail=lambda sql: lost if sql.lstrip().lower().startswith("select") else None)
    conn, made = _pg_conn(first, monkeypatch, fresh=Raw())
    capsys.readouterr()
    cur = conn.execute("SELECT 1")
    assert cur.fetchone()[0] == 1
    assert len(made) == db_pg.RECONNECT_ATTEMPTS == 1     # one reconnect, named constant
    assert first.calls.count("SELECT 1") == 1
    assert made and conn._raw.calls.count("SELECT 1") == 1  # re-run exactly once
    assert capsys.readouterr().err == ""                   # a recovered read logs nothing


def test_v1_pre_write_begin_is_retried_once_not_twice(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    first = Raw(fail=lambda sql: lost if sql.strip().upper() == "BEGIN" else None)
    fresh = Raw()
    conn, made = _pg_conn(first, monkeypatch, fresh=fresh)
    conn.execute("BEGIN IMMEDIATE")
    assert len(made) == db_pg.RECONNECT_ATTEMPTS == 1      # one reconnect
    assert fresh.calls.count("BEGIN") == 1                 # the fresh session took the BEGIN once
    assert conn.execute("SELECT 1").fetchone()[0] == 1     # and is usable afterwards


def test_v1_write_in_a_transaction_is_never_re_run(monkeypatch, capsys):
    psycopg = pytest.importorskip("psycopg")
    lost = psycopg.OperationalError("connection reset by peer")
    fresh = Raw()
    first = Raw(fail=lambda sql: lost if sql.lstrip().lower().startswith("insert") else None)
    conn, _ = _pg_conn(first, monkeypatch, fresh=fresh)
    conn.execute("BEGIN IMMEDIATE")
    capsys.readouterr()
    with pytest.raises(db_pg.ConnectionLost) as exc:
        conn.execute("INSERT INTO audit_log(ts) VALUES(?)", ("t",))
    assert exc.value.fatal is False        # session restored: 503 path, the daemon keeps serving
    writes = [sql for raw in (first, fresh) for sql in raw.calls
              if sql.lstrip().upper().startswith("INSERT")]
    assert len(writes) == 1               # never a second apply of a write
    assert conn.execute("SELECT 1").fetchone()[0] == 1     # next call works on the fresh session
    assert capsys.readouterr().err == ""   # the HTTP layer logs this one, not db_pg


def test_v1_commit_with_an_unknown_outcome_is_never_issued_again(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    fresh = Raw()
    first = Raw(fail=lambda sql: lost if sql.strip().lower().startswith("commit") else None)
    conn, _ = _pg_conn(first, monkeypatch, fresh=fresh)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO items(key) VALUES(?)", ("demo:A",))
    with pytest.raises(db_pg.ConnectionLost) as exc:
        conn.execute("COMMIT")
    assert exc.value.fatal is False
    commits = [sql for raw in (first, fresh) for sql in raw.calls
               if sql.strip().upper().startswith("COMMIT")]
    assert commits == ["COMMIT"]           # an unknown-outcome COMMIT is never re-issued


def test_v1_autocommit_write_is_not_re_run(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    lost = psycopg.OperationalError("connection reset by peer")
    fresh = Raw()
    first = Raw(fail=lambda sql: lost if sql.lstrip().lower().startswith("insert") else None)
    conn, _ = _pg_conn(first, monkeypatch, fresh=fresh)
    with pytest.raises(db_pg.ConnectionLost) as exc:
        conn.execute("INSERT INTO audit_log(ts) VALUES(?)", ("t",))
    assert exc.value.fatal is False
    writes = [sql for raw in (first, fresh) for sql in raw.calls
              if sql.lstrip().upper().startswith("INSERT")]
    assert len(writes) == 1


# ------------------------------------------------------------------- V1 over HTTP
def test_v1_lost_write_transaction_is_a_503_json_answer(doors, w, monkeypatch, capsys):
    seed(w, [("A", [])])
    real = w.store.conn

    class BrokenWrite:
        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("BEGIN"):
                raise db_pg.ConnectionLost(
                    "postgres connection lost inside an unfinished write;"
                    " the statement is not retried")
            return real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(w.store, "conn", BrokenWrite())
    capsys.readouterr()
    status, body = doors.http("router", "POST", "/v1/set_priority",
                              {"key": "demo:A", "n": 1})
    assert status == 503, body
    assert body["error"] == "unavailable"
    assert "ConnectionLost" not in str(body) and "BEGIN" not in str(body)
    assert len(_keeper_lines(capsys.readouterr().err)) == 1


# -------------------------------------------------------------------------- V2
def test_v2_reconnect_failures_log_one_redacted_line_and_are_fatal(monkeypatch, capsys):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    attempts = []

    def _connect(dsn, busy_ms):
        attempts.append(dsn)
        raise lost

    monkeypatch.setattr(db_pg, "_connect", _connect)
    conn = db_pg.PgConnection(Raw(fail=lambda sql: lost), "k_x", dsn=FAKE_DSN, busy_ms=50)
    capsys.readouterr()
    with pytest.raises(db_pg.ConnectionLost) as exc:
        conn.execute("SELECT 1")
    assert exc.value.fatal is True
    assert len(attempts) == db_pg.RECONNECT_ATTEMPTS == 1   # N = the named constant
    err = capsys.readouterr().err
    lines = _keeper_lines(err)
    assert len(lines) == 1 and "reconnect" in lines[0]
    assert "s3cret" not in err and "keeper-db" not in err    # the DSN never reaches a log


def test_v2_retry_that_loses_again_gives_up_after_the_single_reconnect(monkeypatch, capsys):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    attempts = []

    def _connect(dsn, busy_ms):
        attempts.append(dsn)
        return Raw(fail=lambda sql: lost)       # the server accepts, then drops again

    monkeypatch.setattr(db_pg, "_connect", _connect)
    conn = db_pg.PgConnection(Raw(fail=lambda sql: lost), "k_x", dsn=FAKE_DSN, busy_ms=50)
    capsys.readouterr()
    with pytest.raises(db_pg.ConnectionLost) as exc:
        conn.execute("SELECT 1")
    assert exc.value.fatal is True
    assert len(attempts) == db_pg.RECONNECT_ATTEMPTS         # no second reconnect is attempted
    assert len(_keeper_lines(capsys.readouterr().err)) == 1


def _daemon_cfg(tmp_path):
    db_path = tmp_path / "keeper.db"
    kdb.open_db(db_path, create=True).close()
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({
        "db": str(db_path), "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0"}), encoding="utf-8")
    os.chmod(cfg, 0o600)
    return cfg


def test_v2_exhausted_reconnect_exits_the_daemon_non_zero_with_one_line(tmp_path, monkeypatch,
                                                                       capsys):
    psycopg = pytest.importorskip("psycopg")
    cfg = _daemon_cfg(tmp_path)
    lost = _lost(psycopg)

    def _connect(dsn, busy_ms):
        raise lost

    monkeypatch.setattr(db_pg, "_connect", _connect)

    def dead_open(*a, **k):
        conn = db_pg.PgConnection(Raw(fail=lambda sql: lost), "k_x",
                                  dsn=FAKE_DSN, busy_ms=50)
        conn.execute("SELECT 1")        # real exhaustion: db_pg emits the one line
        return conn

    monkeypatch.setattr(kdb, "open_db", dead_open)
    capsys.readouterr()
    assert daemon.main(["--config", str(cfg)]) == 1
    err = capsys.readouterr().err
    assert len(_keeper_lines(err)) == 1, err   # exactly one line for the whole failure
    assert "s3cret" not in err


def test_v2_unlogged_fatal_loss_is_logged_once_by_the_daemon(tmp_path, monkeypatch, capsys):
    cfg = _daemon_cfg(tmp_path)
    loss = db_pg.ConnectionLost(
        "postgres connection lost; not recovered after 1 reconnect attempt(s)", fatal=True)

    def dead_open(*a, **k):
        raise loss

    monkeypatch.setattr(kdb, "open_db", dead_open)
    capsys.readouterr()
    assert daemon.main(["--config", str(cfg)]) == 1
    assert len(_keeper_lines(capsys.readouterr().err)) == 1


# -------------------------------------------------------------------------- V3
def test_v3_handler_exception_is_a_logged_500_json(doors, w, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("cannot reach postgresql://u:s3cret@keeper-db/plangraph (CLOSE-WAIT)")

    monkeypatch.setattr(w.store, "waves", boom)
    capsys.readouterr()
    status, body = doors.http("reader", "GET", "/v1/waves")
    assert status == 500, body
    assert body == {"error": "internal", "msg": "internal server error"}
    err = capsys.readouterr().err
    lines = _keeper_lines(err)
    assert len(lines) == 1, err
    assert "RuntimeError" in lines[0]
    assert "s3cret" not in err and "keeper-db" not in err    # redacted through db_pg.redact


def test_v3_connection_loss_during_authentication_still_gets_a_response(doors, w, monkeypatch,
                                                                       capsys):
    class Dead:
        def execute(self, sql, params=()):
            raise db_pg.ConnectionLost(
                "postgres connection lost; not recovered after 1 reconnect attempt(s)")

    monkeypatch.setattr(w.store, "conn", Dead())
    capsys.readouterr()
    status, body = doors.http("router", "GET", "/v1/whoami")
    assert status == 503, body
    assert body["error"] == "unavailable"
    assert len(_keeper_lines(capsys.readouterr().err)) == 1


def test_v3_exception_during_authentication_still_gets_a_500(doors, w, monkeypatch, capsys):
    class Broken:
        def execute(self, sql, params=()):
            raise RuntimeError("auth store exploded postgresql://u:s3cret@keeper-db/plangraph")

    monkeypatch.setattr(w.store, "conn", Broken())
    capsys.readouterr()
    status, body = doors.http("router", "GET", "/v1/whoami")
    assert status == 500, body
    assert body["error"] == "internal"
    err = capsys.readouterr().err
    lines = _keeper_lines(err)
    assert len(lines) == 1 and "s3cret" not in err


# -------------------------------------------------------------------------- V4
def test_v4_lost_lock_connection_is_a_fatal_loss(monkeypatch, capsys):
    psycopg = pytest.importorskip("psycopg")
    lost = _lost(psycopg)
    lock = db_pg.PgKeeperLock(Raw(fail=lambda sql: lost))
    capsys.readouterr()
    with pytest.raises(db_pg.ConnectionLost) as exc:
        lock.probe()
    assert exc.value.fatal is True     # a lost lock is never re-acquired (split-brain)
    assert capsys.readouterr().err == ""  # the daemon boundary prints the one line


def test_v4_daemon_exits_non_zero_when_the_lock_connection_dies(tmp_path, monkeypatch, capsys):
    cfg = _daemon_cfg(tmp_path)

    class DeadLock:
        probed = 0
        released = 0

        def probe(self):
            self.probed += 1
            raise db_pg.ConnectionLost(
                "postgres keeper lock connection lost; the singleton lock is gone",
                fatal=True)

        def release(self):
            self.released += 1

    dead = DeadLock()
    monkeypatch.setattr(kdb, "lock_keeper", lambda *a, **k: dead)
    capsys.readouterr()
    assert daemon.main(["--config", str(cfg)]) == 1
    assert dead.probed >= 1 and dead.released >= 1   # probed, cleaned up, never kept serving
    assert len(_keeper_lines(capsys.readouterr().err)) == 1


# ------------------------------------------------------- PG_DSN gate (CI)
@needs_pg
def test_terminated_backend_recovers_the_next_authed_call(tmp_path):
    """Kill the keeper's own backend; the next authed call answers 200 within 10 s.

    One reconnect did it (V1 over the real server) -- a keeper that could not
    reconnect would exit non-zero instead of hanging the request.
    """
    import psycopg

    w = World(tmp_path, backend="postgres", dsn=DSN)
    live = Doors(w, tmp_path)
    try:
        seed(w, [("A", [])])
        assert live.http("router", "GET", "/v1/whoami")[0] == 200
        pid = w.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        killer = psycopg.connect(DSN, autocommit=True)
        try:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            killer.close()
        deadline = time.time() + 10.0
        while True:
            try:
                status, body = live.http("router", "GET", "/v1/whoami")
            except Exception:            # a half-closed socket may still cost one attempt
                status, body = None, None
            if status == 200:
                break
            assert time.time() < deadline, f"no 200 within 10 s: {status} {body}"
            time.sleep(0.2)
        status, body = live.http("router", "POST", "/v1/set_priority",
                                 {"key": "demo:A", "n": 2})
        assert status in (200, 204), body       # writes work again on the fresh session
    finally:
        live.close()
        w.close()
