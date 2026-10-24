"""RT-P1-BUS-KEEPER: append-only hash-chained `messages` table.

Write verb `post_message` commits the row before the relay acks, read verb
`messages` selects by trace_id/key/time-range, long-poll GET hints via
pg_notify, jsonl stays metadata-only (D-2024).
"""
from __future__ import annotations

import pytest

from plangraph.keeper import schema
from plangraph.keeper.api_routes import READ_VERBS, WRITE_VERBS
from tests.keeper_util import World

NOW = 1_700_000_000_000


# ---------------------------------------------------------------- unit: schema

def test_messages_table_declared_in_both_ddls():
    assert "messages" in schema.TABLES
    assert "messages" in schema.TABLES_PG
    cols = schema.parse_tables(schema.MESSAGES_DDL)["messages"]
    assert list(cols) == [
        "seq", "from", "to", "body", "trace_id", "key", "run_id",
        "attempt_n", "ack_state", "ts", "prev_hash", "hash",
    ]


def test_messages_indexes_and_ack_trigger_declared():
    idx = schema.parse_indexes(schema.MESSAGES_DDL)
    assert "idx_messages_trace_id" in idx
    assert "idx_messages_key_ts" in idx
    assert "idx_messages_ts" in idx
    trig = schema.parse_triggers(schema.MESSAGES_DDL)
    assert "trg_messages_no_update" in trig
    assert "trg_messages_no_delete" in trig


def test_verbs_declared():
    assert "post_message" in WRITE_VERBS
    assert "messages" in READ_VERBS


# ------------------------------------------------------------------ store: write

def test_post_message_persists_full_linkage(world: World):
    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
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

    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
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
    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
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
    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
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
    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
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
    from plangraph.keeper.store import KeeperStore

    with KeeperStore(world.dbpath) as st:
        st.write("post_message", **{
            "from": "relay", "to": "keeper",
            "body": {"secret": "do-not-log"},
            "trace_id": "t-5", "key": "K-5", "ts": NOW,
        })
    text = (world.dir / "outbox.jsonl").read_text(encoding="utf-8") \
        if (world.dir / "outbox.jsonl").exists() else ""
    assert "do-not-log" not in text
    if text.strip():
        for line in text.strip().splitlines():
            assert "body" not in line or '"body"' not in line


# ------------------------------------------------------- ack only after commit

def test_no_ack_when_insert_fails(world: World, monkeypatch):
    from plangraph.keeper import store as store_mod
    from plangraph.keeper.store import KeeperStore

    def boom(self, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_mod.KeeperStore, "_insert_message", boom)
    with KeeperStore(world.dbpath) as st:
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
