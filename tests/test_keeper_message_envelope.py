"""Tests for keeper message envelope (BUS-ANY-AGENT-CLI W2a)."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from plangraph.fleet_bus.contract import BusError, canonical_post, sign, new_state
from plangraph.fleet_bus.discovery import Host, Registry
from plangraph.keeper import auth, db as kdb, schema
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store
from keeper_util import Clock, World
from test_keeper_api import srv as http_srv, w, call, jbody


NOW = 1_700_000_000


@pytest.fixture
def world(w):
    return w


@pytest.fixture
def srv(http_srv, w):
    class Response:
        def __init__(self, status, raw):
            self.status_code = status
            self.raw = raw

        def json(self):
            return jbody(self.raw)

    class Client:
        def __init__(self):
            self.server = http_srv

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


@pytest.fixture
def host_registry(tmp_path):
    key_file = tmp_path / "host-a.key"
    key_file.write_bytes(b"k" * 32)
    host = Host(
        host="host-a",
        node_id="node-1",
        key_id="ka",
        key_file=key_file,
        endpoint="http://127.0.0.1:8765",
        health="healthy",
    )
    return Registry([host])


def make_signed_envelope(host_name, key_bytes, fields, nonce="nonce-1", msg_id="mid-1", ts=NOW, kid="ka"):
    text = fields["body"] if isinstance(fields["body"], str) else json.dumps(fields["body"], sort_keys=True, separators=(",", ":"))
    wire = {
        "from": host_name,
        "to": fields["to"],
        "ts": ts,
        "nonce": nonce,
        "msg_id": msg_id,
        "kid": kid,
        "text": text,
    }
    sig = sign(key_bytes, canonical_post(wire))
    return {
        "host": host_name,
        "sig": sig,
        "nonce": nonce,
        "ts": ts,
        "msg_id": msg_id,
    }


def test_signed_envelope_verified_success(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields)
    row = world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert row["seq"] == 1
    assert row["from"] == "host-a"
    assert row["msg_id"] == env["msg_id"]


def test_signed_envelope_bad_sig_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"wrong-key" * 4, fields)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "bad_signature"


def test_signed_envelope_unknown_host_refused(world: World, host_registry):
    fields = {"from": "ghost-host", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env = make_signed_envelope("ghost-host", b"k" * 32, fields)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "unknown_kid"


def test_signed_envelope_stale_ts_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": (NOW - 120) * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, ts=NOW - 120)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "skew"


def test_replayed_nonce_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields, nonce="repeat-nonce", msg_id="mid-1")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env1, **fields)

    # Different msg_id, but same nonce -> refused
    fields2 = {"from": "host-a", "to": "keeper", "body": {"hello": "world 2"}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="repeat-nonce", msg_id="mid-2")
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env2, **fields2)
    assert exc.value.code == "replayed_nonce"


def test_nonce_table_append_only_triggers(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="trg-nonce")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        world.conn.execute("UPDATE message_nonces SET host='evil' WHERE nonce='trg-nonce'")

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        world.conn.execute("DELETE FROM message_nonces WHERE nonce='trg-nonce'")


def test_msg_id_retry_same_content_returns_existing_row(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields, nonce="n-1", msg_id="same-mid")
    row1 = world.store.post_message(world.p("router"), registry=host_registry, envelope=env1, **fields)
    assert row1["seq"] == 1

    # Retry with same msg_id, fresh nonce, same content -> returns row1
    env2 = make_signed_envelope("host-a", b"k" * 32, fields, nonce="n-2", msg_id="same-mid")
    row2 = world.store.post_message(world.p("router"), registry=host_registry, envelope=env2, **fields)
    assert row2["seq"] == 1
    assert row2["hash"] == row1["hash"]
    # Verify no second row was inserted
    count = world.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 1


def test_msg_id_retry_different_content_refused(world: World, host_registry):
    fields1 = {"from": "host-a", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-1", msg_id="conflict-mid")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env1, **fields1)

    fields2 = {"from": "host-a", "to": "keeper", "body": {"different": "content"}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="n-2", msg_id="conflict-mid")
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env2, **fields2)
    assert exc.value.code == "conflict"


def test_spoofed_from_refused(world: World, host_registry):
    # Envelope is signed by host-a, but 'from' in message claims host-b
    fields = {"from": "host-b", "to": "keeper", "body": {"hello": "world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, {"from": "host-a", "to": "keeper", "body": fields["body"]})
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code in ("forbidden", "bad_request")


def test_unsigned_posts_by_authenticated_principal_allowed(world: World):
    # Unsigned post without envelope
    fields = {"from": "relay", "to": "keeper", "body": {"op": "ping"}, "ts": NOW * 1000}
    row = world.store.post_message(world.p("router"), **fields)
    assert row["seq"] == 1
    assert row["from"] == "relay"


# --------------------------------------------------------------- API tests

def test_api_post_message_with_envelope(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"op": "api-test"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="api-nonce-1", msg_id="mid-api-1")
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "ok"
    assert out["code"] == 200
    assert out["row"]["seq"] == 1
    assert out["row"]["msg_id"] == "mid-api-1"


def test_api_post_message_bad_sig_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"op": "api-test"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"wrong-key" * 4, fields, nonce="api-nonce-bad", msg_id="mid-api-bad")
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "error"
    assert out["code"] == 401
    assert out["error"] == "bad_signature"


def test_api_post_message_stale_ts_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"op": "api-test"}, "ts": (NOW - 120) * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="api-nonce-stale", msg_id="mid-api-stale", ts=NOW - 120)
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "error"
    assert out["code"] == 401
    assert out["error"] == "skew"


def test_api_post_message_replayed_nonce_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields1 = {"from": "host-a", "to": "keeper", "body": {"n": 1}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="api-repeat-nonce", msg_id="mid-api-r1")
    out1 = srv.call("post_message", dict(fields1, envelope=env1))
    assert out1["status"] == "ok"

    fields2 = {"from": "host-a", "to": "keeper", "body": {"n": 2}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="api-repeat-nonce", msg_id="mid-api-r2")
    out2 = srv.call("post_message", dict(fields2, envelope=env2))
    assert out2["status"] == "error"
    assert out2["code"] == 401
    assert out2["error"] == "replayed_nonce"


def test_api_post_message_idempotent_retry_and_conflict(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields1 = {"from": "host-a", "to": "keeper", "body": {"idempotent": True}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-idem-1", msg_id="mid-idem")
    out1 = srv.call("post_message", dict(fields1, envelope=env1))
    assert out1["status"] == "ok"
    assert out1["row"]["seq"] == 1

    # Retry same msg_id with same content, fresh nonce -> returns existing row
    env2 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-idem-2", msg_id="mid-idem")
    out2 = srv.call("post_message", dict(fields1, envelope=env2))
    assert out2["status"] == "ok"
    assert out2["row"]["seq"] == 1

    # Retry same msg_id with different content -> 409 conflict
    fields3 = {"from": "host-a", "to": "keeper", "body": {"idempotent": False}, "ts": NOW * 1000}
    env3 = make_signed_envelope("host-a", b"k" * 32, fields3, nonce="n-idem-3", msg_id="mid-idem")
    out3 = srv.call("post_message", dict(fields3, envelope=env3))
    assert out3["status"] == "error"
    assert out3["code"] == 409
    assert out3["error"] == "conflict"

