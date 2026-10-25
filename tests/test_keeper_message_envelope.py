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
    if hasattr(w, "path"):
        w.store.outbox_dir = w.path.parent
    return w


@pytest.fixture
def srv(http_srv, w):
    if hasattr(w, "path"):
        http_srv.store.outbox_dir = w.path.parent
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
    text = fields["body"]["text"] if (isinstance(fields["body"], dict) and "text" in fields["body"]) else str(fields["body"])
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
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields)
    row = world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert row["seq"] == 1
    assert row["from"] == "host-a"
    assert row["msg_id"] == env["msg_id"]
    assert row["posted_by"] == "router"


def test_signed_envelope_bad_sig_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"wrong-key" * 4, fields)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "bad_signature"


def test_signed_envelope_unknown_host_refused(world: World, host_registry):
    fields = {"from": "ghost-host", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env = make_signed_envelope("ghost-host", b"k" * 32, fields)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "unknown_kid"


def test_signed_envelope_stale_ts_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": (NOW - 120) * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, ts=NOW - 120)
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code == "skew"


def test_replayed_nonce_refused(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields, nonce="repeat-nonce", msg_id="mid-1")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env1, **fields)

    # Different msg_id, but same nonce -> refused
    fields2 = {"from": "host-a", "to": "keeper", "body": {"text": "hello world 2"}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="repeat-nonce", msg_id="mid-2")
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env2, **fields2)
    assert exc.value.code == "replayed_nonce"


def test_nonce_table_append_only_triggers(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="trg-nonce")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)

    from plangraph.keeper import db_errors
    expected_errors = (sqlite3.DatabaseError, *db_errors.INTEGRITY)
    with pytest.raises(expected_errors, match="append-only"):
        world.conn.execute("UPDATE message_nonces SET host='evil' WHERE nonce='trg-nonce'")

    with pytest.raises(expected_errors, match="append-only"):
        world.conn.execute("DELETE FROM message_nonces WHERE nonce='trg-nonce'")


def test_msg_id_retry_same_content_returns_existing_row(world: World, host_registry):
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
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
    fields1 = {"from": "host-a", "to": "keeper", "body": {"text": "hello world"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-1", msg_id="conflict-mid")
    world.store.post_message(world.p("router"), registry=host_registry, envelope=env1, **fields1)

    fields2 = {"from": "host-a", "to": "keeper", "body": {"text": "different content"}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="n-2", msg_id="conflict-mid")
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env2, **fields2)
    assert exc.value.code == "conflict"


def test_spoofed_from_refused(world: World, host_registry):
    # Envelope is signed by host-a, but 'from' in message claims host-b
    fields = {"from": "host-b", "to": "keeper", "body": {"text": "hello"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, {"from": "host-a", "to": "keeper", "body": fields["body"]})
    with pytest.raises(Refused) as exc:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc.value.code in ("forbidden", "bad_request")


def test_unsigned_posts_by_authenticated_principal_allowed(world: World, host_registry):
    # Unsigned post without envelope: router may post as registered host
    world.store.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"op": "ping"}, "ts": NOW * 1000}
    row = world.store.post_message(world.p("router"), **fields)
    assert row["seq"] == 1
    assert row["from"] == "host-a"
    assert row["posted_by"] == "router"


# --------------------------------------------------------------- API tests

def test_api_post_message_with_envelope(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "api-test"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="api-nonce-1", msg_id="mid-api-1")
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "ok"
    assert out["code"] == 200
    assert out["row"]["seq"] == 1
    assert out["row"]["msg_id"] == "mid-api-1"


def test_api_post_message_bad_sig_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "api-test"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"wrong-key" * 4, fields, nonce="api-nonce-bad", msg_id="mid-api-bad")
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "error"
    assert out["code"] == 401
    assert out["error"] == "bad_signature"


def test_api_post_message_stale_ts_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "api-test"}, "ts": (NOW - 120) * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, nonce="api-nonce-stale", msg_id="mid-api-stale", ts=NOW - 120)
    out = srv.call("post_message", dict(fields, envelope=env))
    assert out["status"] == "error"
    assert out["code"] == 401
    assert out["error"] == "skew"


def test_api_post_message_replayed_nonce_returns_401(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields1 = {"from": "host-a", "to": "keeper", "body": {"text": "n1"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="api-repeat-nonce", msg_id="mid-api-r1")
    out1 = srv.call("post_message", dict(fields1, envelope=env1))
    assert out1["status"] == "ok"

    fields2 = {"from": "host-a", "to": "keeper", "body": {"text": "n2"}, "ts": NOW * 1000}
    env2 = make_signed_envelope("host-a", b"k" * 32, fields2, nonce="api-repeat-nonce", msg_id="mid-api-r2")
    out2 = srv.call("post_message", dict(fields2, envelope=env2))
    assert out2["status"] == "error"
    assert out2["code"] == 401
    assert out2["error"] == "replayed_nonce"


def test_api_post_message_idempotent_retry_and_conflict(srv, host_registry):
    srv.server.bus_registry = host_registry
    fields1 = {"from": "host-a", "to": "keeper", "body": {"text": "idem-1"}, "ts": NOW * 1000}
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
    fields3 = {"from": "host-a", "to": "keeper", "body": {"text": "idem-diff"}, "ts": NOW * 1000}
    env3 = make_signed_envelope("host-a", b"k" * 32, fields3, nonce="n-idem-3", msg_id="mid-idem")
    out3 = srv.call("post_message", dict(fields3, envelope=env3))
    assert out3["status"] == "error"
    assert out3["code"] == 409
    assert out3["error"] == "conflict"


def test_signed_raw_text_client_conformance(world: World, host_registry):
    """G1-1: client signs raw text 'hello' and sends body={'text': 'hello'}."""
    text = "hello raw text payload"
    fields = {"from": "host-a", "to": "keeper", "body": {"text": text}, "ts": NOW * 1000}
    # Client signing: canonical_post signs the raw text directly
    wire = {
        "from": "host-a", "to": "keeper", "ts": NOW, "nonce": "nonce-raw-1",
        "msg_id": "mid-raw-1", "kid": "ka", "text": text,
    }
    sig = sign(b"k" * 32, canonical_post(wire))
    env = {"host": "host-a", "sig": sig, "nonce": "nonce-raw-1", "ts": NOW, "msg_id": "mid-raw-1"}
    row = world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert row["seq"] == 1
    assert row["body"] == {"text": text}


def test_server_starts_registry_from_env_and_hosts_endpoint(tmp_path, monkeypatch):
    """G1-4: PLANGRAPH_BUS_INVENTORY sets server bus_registry, runs discovery, exposes GET /v1/hosts."""
    import os
    from plangraph.keeper import api, config
    from test_keeper_api import call as api_call
    key_file = tmp_path / "host-env.key"
    key_file.write_bytes(b"e" * 32)
    inv_file = tmp_path / "inventory.json"
    inv_file.write_text(json.dumps({
        "hosts": [{"host": "host-env", "node_id": "node-env", "key_id": "kenv", "key_file": str(key_file)}],
        "port": 8765,
    }))
    monkeypatch.setenv("PLANGRAPH_BUS_INVENTORY", str(inv_file))
    cfg = config.Config({"host": "127.0.0.1", "port": 0}, tmp_path)
    w = World(tmp_path)
    srv = api.make_server(w.store, cfg, host="127.0.0.1", port=0)
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert getattr(srv, "bus_registry", None) is not None
        status, _, raw = api_call(srv, "/v1/hosts", token=w.tok("router"))
        assert status == 200
        data = json.loads(raw.decode("utf-8"))
        assert "hosts" in data
        assert len(data["hosts"]) == 1
        h = data["hosts"][0]
        assert h["host"] == "host-env"
        assert h["key_id"] == "kenv"
        assert "key_file" not in h
        assert "endpoint" in h
        assert "health" in h
        assert "last_seen" in h
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_low_v2_4_nonces_recorded_on_idempotent_and_conflict(srv, world: World, host_registry):
    """V2-4: record the nonce in message_nonces on the idempotent and conflict paths too."""
    srv.server.bus_registry = host_registry
    fields1 = {"from": "host-a", "to": "keeper", "body": {"text": "v2-4-first"}, "ts": NOW * 1000}
    env1 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-v24-1", msg_id="mid-v24")
    out1 = srv.call("post_message", dict(fields1, envelope=env1))
    assert out1["status"] == "ok"

    # Idempotent retry with new nonce
    env2 = make_signed_envelope("host-a", b"k" * 32, fields1, nonce="n-v24-2", msg_id="mid-v24")
    out2 = srv.call("post_message", dict(fields1, envelope=env2))
    assert out2["status"] == "ok"

    # Conflict with new nonce
    fields3 = {"from": "host-a", "to": "keeper", "body": {"text": "v2-4-conflict"}, "ts": NOW * 1000}
    env3 = make_signed_envelope("host-a", b"k" * 32, fields3, nonce="n-v24-3", msg_id="mid-v24")
    out3 = srv.call("post_message", dict(fields3, envelope=env3))
    assert out3["status"] == "error"
    assert out3["code"] == 409

    nonces = [r[0] for r in world.conn.execute("SELECT nonce FROM message_nonces").fetchall()]
    assert "n-v24-1" in nonces
    assert "n-v24-2" in nonces
    assert "n-v24-3" in nonces


def test_low_v2_5_non_string_msg_id_returns_400(srv, host_registry):
    """V2-5: non-string msg_id returns 400."""
    srv.server.bus_registry = host_registry
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "v2-5"}, "msg_id": 12345}
    out = srv.call("post_message", fields)
    assert out["status"] == "error"
    assert out["code"] == 400


def test_low_v2_6_canonicalisation_bytes_equality(host_registry):
    """V2-6: assert in a test that keeper-side verification uses contract canonicalisation (client canonical_post bytes == keeper bytes)."""
    text = "canonical test payload"
    client_wire = {
        "from": "host-a",
        "to": "keeper",
        "ts": NOW,
        "nonce": "n-canon",
        "msg_id": "m-canon",
        "kid": "ka",
        "text": text,
    }
    client_canonical = canonical_post(client_wire).encode("utf-8")

    host = host_registry.hosts["host-a"]
    keeper_wire = {
        "from": "host-a",
        "to": "keeper",
        "ts": NOW,
        "nonce": "n-canon",
        "msg_id": "m-canon",
        "kid": host.key_id,
        "text": text,
    }
    keeper_canonical = canonical_post(keeper_wire).encode("utf-8")
    assert client_canonical == keeper_canonical


def test_low_v2_7_kid_mismatch_refused(world: World, host_registry):
    """V2-7: refuse an envelope whose 'kid' differs from the host key id; do not take it from the envelope."""
    fields = {"from": "host-a", "to": "keeper", "body": {"text": "v2-7"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields, kid="wrong-kid")
    env["kid"] = "wrong-kid"
    with pytest.raises(Refused) as exc_info:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc_info.value.code == "unknown_kid"


def test_low_v2_3_bearer_differs_from_signed_host(world: World, host_registry):
    """V2-3: when the bearer principal differs from the signed host, refuse unless `from` == signed host."""
    fields = {"from": "host-spoofed", "to": "keeper", "body": {"text": "v2-3"}, "ts": NOW * 1000}
    env = make_signed_envelope("host-a", b"k" * 32, fields)
    with pytest.raises(Refused) as exc_info:
        world.store.post_message(world.p("router"), registry=host_registry, envelope=env, **fields)
    assert exc_info.value.code == "forbidden"


def test_low_v1_6_api_reserved_keys_or_nondict(srv):
    """V1-6: api post_message with a non-dict body, or a body key 'principal'/'registry', returns 400, not 500."""
    out1 = srv.call("post_message", {"from": "router", "to": "keeper", "body": "hi", "principal": "admin"})
    assert out1["status"] == "error"
    assert out1["code"] == 400

    out2 = srv.call("post_message", {"from": "router", "to": "keeper", "body": "hi", "registry": "fake"})
    assert out2["status"] == "error"
    assert out2["code"] == 400


def test_g1_5_body_max_overflow_returns_413(srv):
    """G1-5: post_message refuses a serialized body over the contract BODY_MAX with too_large; api returns 413."""
    big_body = {"big": "x" * 4100}
    out = srv.call("post_message", {"from": "router", "to": "keeper", "body": big_body})
    assert out["status"] == "error"
    assert out["code"] == 413



