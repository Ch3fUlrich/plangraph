"""RT-P1-BUS-KEEPER: append-only hash-chained `messages` table.

Write verb `post_message` commits the row before the relay acks, read verb
`messages` selects by trace_id/key/time-range, long-poll GET hints via
pg_notify, jsonl stays metadata-only (D-2024).
"""
from __future__ import annotations

import os
import pytest

from plangraph.keeper import schema
from plangraph.keeper.api_routes import READ_VERBS, WRITE_VERBS
from tests.keeper_util import World
from test_keeper_api import srv as http_srv, w, call, jbody
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store
from contextlib import contextmanager


@pytest.fixture
def world(w):
    return w


@contextmanager
def message_store(world):
    class Adapter:
        def write(self, verb, **fields):
            try:
                return world.store.post_message(world.p("router"), **fields)
            except Refused as exc:
                raise ValueError(exc.msg) from exc

        def read(self, verb, **filters):
            return world.store.messages(world.p("router"), **filters)
    yield Adapter()


@pytest.fixture
def srv(http_srv, w):
    class Response:
        def __init__(self, status, raw):
            self.status_code = status
            self.raw = raw

        def json(self):
            return jbody(self.raw)

    class Client:
        def get(self, path):
            status, _, raw = call(http_srv, "/v1" + path, token=w.tok("router"))
            return Response(status, raw)

        def call(self, verb, fields):
            from urllib.parse import urlencode
            if verb == "messages":
                response = self.get("/messages?" + urlencode(fields))
            else:
                status, _, raw = call(http_srv, "/v1/" + verb, method="POST",
                                      body=fields, token=w.tok("router"))
                response = Response(status, raw)
            data = response.json()
            return dict(data, status="ok" if response.status_code == 200 else "error",
                        code=response.status_code)
    return Client()

NOW = 1_700_000_000_000


# ---------------------------------------------------------------- unit: schema

def test_messages_table_declared_in_both_ddls():
    from plangraph.keeper import schema_pg
    import re
    assert "messages" in schema.SCHEMA
    assert "messages" in schema_pg.MIGRATIONS[-1].sql
    m_body = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?messages \(\n(.*?)\n\);", schema.MESSAGES_DDL, re.S)
    cols = re.findall(r'^    "?(\w+)"? \w+', m_body.group(1), re.M)
    assert list(cols) == [
        "seq", "from", "to", "body", "trace_id", "key", "run_id",
        "attempt_n", "ack_state", "ts", "prev_hash", "hash",
    ]
    assert "message_nonces" in schema.SCHEMA
    assert "message_envelopes" in schema.SCHEMA


def test_messages_indexes_and_ack_trigger_declared():
    idx = schema.MESSAGES_DDL
    assert "idx_messages_trace_id" in idx
    assert "idx_messages_key_ts" in idx
    assert "idx_messages_ts" in idx
    trig = schema.MESSAGES_DDL
    assert "trg_messages_no_update" in trig
    assert "trg_messages_no_delete" in trig


def test_verbs_declared():
    assert "post_message" in WRITE_VERBS
    assert "messages" in READ_VERBS


# ------------------------------------------------------------------ store: write

def test_post_message_persists_full_linkage(world: World):

    with message_store(world) as st:
        row = st.write(
            "post_message",
            **{
                "from": "relay", "to": "keeper", "body": {"op": "ingest"},
                "trace_id": "t-1", "key": "K-9", "run_id": "R-7",
                "attempt_n": 2, "ts": NOW,
            },
        )
    assert row["seq"] == 1
    assert row["trace_id"] == "t-1"
    assert row["key"] == "K-9"
    assert row["run_id"] == "R-7"
    assert row["attempt_n"] == 2
    assert row["ack_state"] == "pending"
    assert row["ts"] == NOW
    assert row["hash"] and row["prev_hash"] == "0" * 64


def test_post_message_hash_chain_links_rows(world: World):
    import hashlib
    import json


    with message_store(world) as st:
        r1 = st.write("post_message", **{
            "from": "a", "to": "b", "body": {"n": 1},
            "trace_id": "t-1", "ts": NOW,
        })
        r2 = st.write("post_message", **{
            "from": "a", "to": "b", "body": {"n": 2},
            "trace_id": "t-1", "ts": NOW + 1,
        })
    assert r2["prev_hash"] == r1["hash"]
    payload = {
        "seq": 2, "from": "a", "to": "b", "body": {"n": 2},
        "trace_id": "t-1", "key": "", "run_id": "", "attempt_n": 0,
        "ack_state": "pending", "ts": NOW + 1, "prev_hash": r1["hash"],
    }
    want = hashlib.sha256(
        (r1["hash"] + json.dumps(payload, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=True)).encode()
    ).hexdigest()
    assert r2["hash"] == want


def test_post_message_rejects_bad_enum_and_types(world: World):

    with message_store(world) as st:
        with pytest.raises(ValueError):
            st.write("post_message", **{
                "from": "relay", "to": "nope", "body": {}, "ts": NOW,
            })
        with pytest.raises(ValueError):
            st.write("post_message", **{
                "from": "relay", "to": "keeper", "body": {}, "ts": "now",
            })
        with pytest.raises(ValueError):
            st.write("post_message", **{
                "from": "relay", "to": "keeper", "body": {},
                "attempt_n": "x", "ts": NOW,
            })


# ------------------------------------------------------------------ store: read

def test_messages_read_filters_by_trace_key_and_range(world: World):

    with message_store(world) as st:
        for i, (tid, key) in enumerate([
            ("t-1", "K-1"), ("t-1", "K-2"), ("t-2", "K-1"),
        ]):
            st.write("post_message", **{
                "from": "a", "to": "keeper", "body": {"i": i},
                "trace_id": tid, "key": key, "ts": NOW + i * 1000,
            })
        by_trace = st.read("messages", trace_id="t-1")
        assert [m["key"] for m in by_trace] == ["K-1", "K-2"]
        by_key = st.read("messages", key="K-1")
        assert [m["trace_id"] for m in by_key] == ["t-1", "t-2"]
        by_range = st.read("messages", ts_min=NOW + 500, ts_max=NOW + 1500)
        assert [m["seq"] for m in by_range] == [2]
        by_run = st.read("messages", run_id="R-9")
        assert by_run == []


def test_messages_read_returns_body_and_linkage(world: World):

    with message_store(world) as st:
        st.write("post_message", **{
            "from": "relay", "to": "keeper", "body": {"op": "ping"},
            "trace_id": "t-9", "key": "K-9", "run_id": "R-9",
            "attempt_n": 3, "ts": NOW,
        })
        rows = st.read("messages", trace_id="t-9")
    assert len(rows) == 1
    row = rows[0]
    assert row["body"] == {"op": "ping"}
    assert (row["run_id"], row["attempt_n"], row["key"]) == ("R-9", 3, "K-9")
    assert row["ack_state"] == "pending"


# ----------------------------------------------------------------- jsonl: no body

def test_jsonl_outbox_stays_metadata_only(world: World):

    with message_store(world) as st:
        st.write("post_message", **{
            "from": "relay", "to": "keeper",
            "body": {"secret": "do-not-log"},
            "trace_id": "t-5", "key": "K-5", "ts": NOW,
        })
    text = (world.path.parent / "outbox.jsonl").read_text(encoding="utf-8") \
        if (world.path.parent / "outbox.jsonl").exists() else ""
    assert "do-not-log" not in text
    if text.strip():
        for line in text.strip().splitlines():
            assert "body" not in line or '"body"' not in line


# ------------------------------------------------------- ack only after commit

def test_no_ack_when_insert_fails(world: World, monkeypatch):
    from plangraph.keeper import store as store_mod

    def boom(self, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_mod.Store, "_insert_message", boom)
    with message_store(world) as st:
        with pytest.raises(RuntimeError):
            st.write("post_message", **{
                "from": "relay", "to": "keeper", "body": {},
                "trace_id": "t-x", "ts": NOW,
            })
        assert st.read("messages") == []


# --------------------------------------------------------------- api verbs

def test_api_post_message_then_read(srv):
    out = srv.call("post_message", {
        "from": "relay", "to": "keeper", "body": {"op": "ingest"},
        "trace_id": "t-api", "key": "K-API", "run_id": "R-API",
        "attempt_n": 1, "ts": NOW,
    })
    assert out["status"] == "ok"
    assert out["row"]["seq"] == 1
    got = srv.call("messages", {"trace_id": "t-api"})
    assert got["status"] == "ok"
    assert got["rows"][0]["key"] == "K-API"
    assert got["rows"][0]["body"] == {"op": "ingest"}


def test_api_post_message_rejects_unknown_to(srv):
    out = srv.call("post_message", {
        "from": "relay", "to": "ghost", "body": {}, "ts": NOW,
    })
    assert out["status"] == "error"
    assert out["code"] == 400


def test_api_messages_requires_filter(srv):
    out = srv.call("messages", {})
    assert out["status"] == "error"
    assert out["code"] == 400


# ---------------------------------------------------------------- long poll

def test_long_poll_get_returns_rows_after_last_seq(srv):
    srv.call("post_message", {
        "from": "relay", "to": "keeper", "body": {"n": 1},
        "trace_id": "t-lp", "ts": NOW,
    })
    resp = srv.get("/messages?since=0&wait_ms=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"][0]["trace_id"] == "t-lp"
    assert data["rows"][0]["seq"] == 1
    # re-read after last seq yields only newer rows
    srv.call("post_message", {
        "from": "relay", "to": "keeper", "body": {"n": 2},
        "trace_id": "t-lp", "ts": NOW + 1,
    })
    resp2 = srv.get("/messages?since=1&wait_ms=0")
    rows = resp2.json()["rows"]
    assert [r["seq"] for r in rows] == [2]


def test_long_poll_get_empty_beyond_last_seq(srv):
    srv.call("post_message", {
        "from": "relay", "to": "keeper", "body": {},
        "trace_id": "t-lp0", "ts": NOW,
    })
    resp = srv.get("/messages?since=1&wait_ms=0")
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_long_poll_get_requires_since(srv):
    resp = srv.get("/messages")
    assert resp.status_code == 400


def test_append_only_enforced(world):
    with message_store(world) as st:
        st.write("post_message", **{"from": "relay", "to": "keeper", "body": {}})
    from plangraph.keeper import db_errors
    for sql in ("UPDATE messages SET body='{}'", "DELETE FROM messages"):
        with pytest.raises(db_errors.INTEGRITY):
            world.conn.execute(sql)


def test_commit_failure_never_returns_receipt(world, monkeypatch):
    original = world.conn.execute

    def execute(sql, *args):
        if sql == "COMMIT":
            raise RuntimeError("commit failed")
        return original(sql, *args)

    class Proxy:
        def execute(self, sql, *args):
            return execute(sql, *args)
    monkeypatch.setattr(world.store, "conn", Proxy())
    with pytest.raises(RuntimeError):
        world.store.post_message(world.p("router"), **{
            "from": "relay", "to": "keeper", "body": {}})
    assert not world.conn.in_transaction
    assert world.store.messages(world.p("router")) == []


def test_wait_releases_lock_and_rereads_without_notify(srv):
    import threading
    import time
    result = []
    thread = threading.Thread(target=lambda: result.append(
        srv.get("/messages?since=0&wait_ms=1000")))
    thread.start()
    try:
        time.sleep(.1)
        srv.call("post_message", {"from": "relay", "to": "keeper", "body": {}})
    finally:
        thread.join(3)
    assert not thread.is_alive()
    assert result[0].json()["rows"][0]["seq"] == 1


def test_reader_cannot_post(world):
    with pytest.raises(Refused):
        world.store.post_message(world.p("reader"), **{
            "from": "relay", "to": "keeper", "body": {}})


def test_api_relay_ack_only_after_commit(srv, w, monkeypatch):
    """(a) The relay acks a POST only after the keeper row commits."""
    original = w.conn.execute

    def execute(sql, *args):
        if sql == "COMMIT":
            raise RuntimeError("commit failed")
        return original(sql, *args)

    class Proxy:
        def execute(self, sql, *args):
            return execute(sql, *args)

        def __getattr__(self, name):
            return getattr(w.conn, name)

    monkeypatch.setattr(w.store, "conn", Proxy())
    out = srv.call("post_message", {
        "from": "relay", "to": "keeper", "body": {"m": 1},
        "trace_id": "t-ack-fail", "ts": NOW,
    })
    assert out["status"] == "error"
    assert out["code"] == 500
    assert "seq" not in out
    got = srv.call("messages", {"trace_id": "t-ack-fail"})
    assert got["status"] == "ok"
    assert got["rows"] == []


def test_long_poll_direct_connection_and_lost_notify_loses_nothing(world, monkeypatch):
    """(b) pg_notify on insert is only a hint: the long-poll waits on a DIRECT (unpooled)
    connection and re-reads after the last seq, so a lost NOTIFY loses nothing."""
    import types
    from plangraph.keeper.messages_poll import read_messages

    direct_dsns = []
    listen_called = []

    class FakeNotifies:
        def __init__(self):
            self.calls = 0

        def notifies(self, timeout, stop_after=1):
            self.calls += 1
            return iter([])

    class FakeListener:
        def __init__(self, dsn):
            self.dsn = dsn
            direct_dsns.append(dsn)
            self._fake_notifies = FakeNotifies()

        def execute(self, sql):
            listen_called.append(sql)

        def notifies(self, timeout, stop_after=1):
            return self._fake_notifies.notifies(timeout, stop_after)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda dsn, autocommit=True: FakeListener(dsn)
    monkeypatch.setitem(__import__("sys").modules, "psycopg", fake_psycopg)

    class RawInfo:
        dsn = "postgresql://direct-host:5432/keeper_db"

    class RawConn:
        info = RawInfo()

    class ConnProxy:
        def __init__(self, conn):
            self._conn = conn
            self._raw = RawConn()

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

    monkeypatch.setattr(world.store, "conn", ConnProxy(world.conn))

    class FakeLock:
        def release(self):
            pass

        def acquire(self):
            pass

    server = types.SimpleNamespace(store=world.store, lock=FakeLock())

    world.store.post_message(world.p("router"), **{
        "from": "relay", "to": "keeper", "body": {"m": 1},
        "trace_id": "t-b", "ts": NOW,
    })

    real_messages = world.store.messages
    calls = []

    def spy_messages(principal, **filters):
        calls.append(filters)
        if len(calls) == 1:
            return []
        world.store.post_message(world.p("router"), **{
            "from": "relay", "to": "keeper", "body": {"m": 2},
            "trace_id": "t-b", "ts": NOW + 1,
        })
        return real_messages(principal, **filters)

    monkeypatch.setattr(world.store, "messages", spy_messages)

    res = read_messages(server, world.p("router"), {"since": ["1"], "wait_ms": ["500"]})
    assert direct_dsns == ["postgresql://direct-host:5432/keeper_db"]
    assert "LISTEN keeper_messages" in listen_called
    assert len(res["rows"]) == 1
    assert res["rows"][0]["seq"] == 2


def test_jsonl_and_audit_stay_metadata_only(world: World):
    """(c) jsonl stays metadata-only (no body)."""
    hidden_data = "unlogged_body_value_xyz"
    with message_store(world) as st:
        st.write("post_message", **{
            "from": "relay", "to": "keeper",
            "body": {"payload": hidden_data},
            "trace_id": "t-c-meta", "key": "K-meta", "ts": NOW,
        })
    for path in world.path.parent.glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        assert hidden_data not in text
    audit_rows = world.conn.execute(
        "SELECT * FROM audit_log WHERE key='K-meta'").fetchall()
    assert len(audit_rows) >= 1
    for row in audit_rows:
        row_dict = dict(row)
        for val in row_dict.values():
            assert hidden_data not in str(val)


def test_messages_read_filters_via_api(srv):
    """(d) The read verb filters by trace_id, key and time range."""
    for i in range(4):
        srv.call("post_message", {
            "from": "relay", "to": "keeper", "body": {"idx": i},
            "trace_id": f"trace-{i % 2}", "key": f"K-{i // 2}",
            "ts": NOW + i * 100,
        })
    res_trace = srv.call("messages", {"trace_id": "trace-0"})
    assert res_trace["status"] == "ok"
    assert [m["seq"] for m in res_trace["rows"]] == [1, 3]

    res_key = srv.call("messages", {"key": "K-1"})
    assert res_key["status"] == "ok"
    assert [m["seq"] for m in res_key["rows"]] == [3, 4]

    res_time = srv.call("messages", {"key": "K-0", "ts_min": NOW + 50, "ts_max": NOW + 150})
    assert res_time["status"] == "ok"
    assert [m["seq"] for m in res_time["rows"]] == [2]


def test_post_message_links_item_key_run_and_attempt(world: World):
    """(e) Each row links item key + run/attempt; attempt_n validated."""
    with message_store(world) as st:
        row = st.write("post_message", **{
            "from": "worker", "to": "keeper", "body": {"done": True},
            "trace_id": "tr-link", "key": "task:build-L1",
            "run_id": "run-999", "attempt_n": 7, "ts": NOW,
        })
        assert row["key"] == "task:build-L1"
        assert row["run_id"] == "run-999"
        assert row["attempt_n"] == 7

        with pytest.raises(ValueError):
            st.write("post_message", **{
                "from": "worker", "to": "keeper", "body": {},
                "key": "task:build-L1", "run_id": "run-999", "attempt_n": -1, "ts": NOW,
            })


def test_append_only_refused_on_sqlite_and_pg(world, tmp_path):
    """(f) Append-only: UPDATE and DELETE refused on both SQLite and PG."""
    from plangraph.keeper import db_errors
    with message_store(world) as st:
        st.write("post_message", **{"from": "relay", "to": "keeper", "body": {"v": 1}})
    for sql in ("UPDATE messages SET body='{}'", "DELETE FROM messages WHERE seq=1", "DELETE FROM messages"):
        with pytest.raises(db_errors.INTEGRITY):
            world.conn.execute(sql)

    dsn = os.environ.get("PG_DSN")
    if dsn:
        w_pg = World(tmp_path / "pg_app", backend="postgres", dsn=dsn)
        try:
            w_pg.store.post_message(w_pg.p("router"), **{"from": "relay", "to": "keeper", "body": {"v": 1}})
            for sql in ("UPDATE messages SET body='{}'", "DELETE FROM messages WHERE seq=1", "DELETE FROM messages"):
                with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
                    w_pg.conn.execute(sql)
        finally:
            w_pg.close()
