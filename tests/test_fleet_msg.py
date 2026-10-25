"""G2 (FIX ROUND 1) tests for ``fleet-msg`` / ``plangraph.fleet_bus.client``.

Every write-path runs against a REAL keeper (``World`` + ``keeper.api.make_server``
over loopback HTTP) instead of the old fake router, so the envelope the keeper
*accepts* — verified with the same ``Registry.validate_post`` / canonical form
the wire contract uses — is the oracle for the client's wire form.  Only the
503/timeout paths use tiny fakes.

Findings covered: G2-1..G2-8, V6-6..V6-9, V7-3.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.fleet_bus import contract, discovery
from plangraph.fleet_bus.client import (
    PROBE_TIMEOUT_S,
    BusClient,
    Config,
    HttpTransport,
    Refused,
    Spool,
    _health,
    _identity_ok,
    _key_path,
    _Prober,
    _wire_text,
    build_client,
    call_tool,
    load_config,
)
from plangraph.keeper import api
from keeper_util import World
from test_keeper_api import make_cfg

REPO = Path(__file__).resolve().parents[1]

#: Every name the keeper's ``post_message`` accepts as a recipient, plus the
#: senders used here.  One real keeper plays all of these roles.
HOSTS = ["keeper", "relay", "router", "worker", "l1", "a", "b"]


def _secret(name):
    """The HMAC key bytes in a host's key file (>= 32 bytes)."""
    return f"g2-secret-{name}-".encode() + b"x" * 32


@pytest.fixture()
def bus(tmp_path):
    """A real keeper on 127.0.0.1:<ephemeral> plus a client-side inventory.

    The server-side ``bus_registry`` and the client-side inventory reference the
    same key files and ``key_id`` values, so a round trip proves the client
    signs exactly what the keeper verifies.
    """
    w = World(tmp_path)
    w.clock.t = time.time()   # the keeper's skew gate must agree with wall time
    keys = tmp_path / "keys"
    keys.mkdir()
    for name in HOSTS:
        (keys / f"{name}.key").write_bytes(_secret(name))

    cfg = make_cfg(tmp_path, w)
    srv = api.make_server(w.store, cfg, port=0)
    port = srv.server_address[1]
    registry = discovery.Registry(
        [discovery.Host(host=name, node_id=f"node-{name}", key_id=f"kid-{name}",
                        key_file=keys / f"{name}.key") for name in HOSTS],
        port=port,
        on_refresh=srv._persist_bus_hosts)
    srv._restore_bus_hosts(registry)
    srv.bus_registry = registry
    srv.store.bus_registry = registry
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    root = tmp_path / "client"
    root.mkdir()
    inventory = {
        "port": port,
        "hosts": [{"host": name, "node_id": f"node-{name}",
                   "key_id": f"kid-{name}", "key_file": str(keys / f"{name}.key")}
                  for name in HOSTS],
        "agents": {},
    }
    (root / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    status = {"Peer": {f"node-{name}": {"Online": True, "DNSName": "127.0.0.1"}
                       for name in HOSTS}}
    (root / "status.json").write_text(json.dumps(status), encoding="utf-8")
    for name in HOSTS:
        tok = w.tokens[name] if name in w.tokens else w.tok("router")
        (root / f"token-{name}.txt").write_text(tok, encoding="utf-8")

    url = f"http://127.0.0.1:{port}"

    def env(host="router"):
        return {"FLEET_MSG_INVENTORY": str(root / "inventory.json"),
                "FLEET_MSG_HOST": host,
                "FLEET_MSG_SPOOL": str(root / f"spool-{host}"),
                "FLEET_MSG_STATUS_FILE": str(root / "status.json"),
                "PG_TOKEN_FILE": str(root / f"token-{host}.txt")}

    def fetch_rows(who="router", since=0):
        """GET /v1/messages as ``who`` — the keeper's own view of the rows."""
        request = urllib.request.Request(
            f"{url}/v1/messages?since={since}",
            headers={"Authorization": f"Bearer {w.tok(who)}"})
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))["rows"]

    yield SimpleNamespace(
        w=w, srv=srv, url=url, port=port, keys=keys, root=root,
        registry=registry, env=env, fetch_rows=fetch_rows,
        token=lambda name: w.tok(name), cfg=cfg)
    srv.shutdown()
    srv.server_close()
    w.close()


RUNNER = (
    "import sys; "
    f"sys.path.insert(0, {str(REPO / 'src')!r}); "
    "from plangraph.fleet_bus.client import main; "
    "raise SystemExit(main(sys.argv[1:]))"
)


def run_cli(bus, args, *, host="router"):
    """Run the real CLI in a subprocess with the fixture's inventory."""
    env = dict(os.environ)
    env.update(bus.env(host))
    return subprocess.run([sys.executable, "-c", RUNNER, *args],
                          capture_output=True, text=True, env=env, timeout=60)


class FakeTransport:
    """Answers ``GET /v1/health`` with a fixed parsed body (or a failure)."""

    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.urls = []

    def request(self, url, **_kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.body


def _capture_posts(monkeypatch):
    """Record the payload the real keeper accepted on POST /v1/post_message."""
    captured = {}
    real_request = HttpTransport.request

    def spy(self, url, *, method="GET", payload=None, query=None, **kwargs):
        if payload is not None:
            captured["payload"] = payload
        return real_request(self, url, method=method, payload=payload,
                            query=query, **kwargs)

    monkeypatch.setattr("plangraph.fleet_bus.client.HttpTransport.request",
                        spy)
    return captured


# --------------------------------------------------------------------------
# G2-1: the write-paths run against a REAL keeper, not a fake router
# --------------------------------------------------------------------------

def test_send_roundtrip_through_real_keeper(bus):
    client = build_client(bus.env())
    result = client.send("worker", "hello over the real bus")
    assert result["queued"] is False
    rows = bus.fetch_rows("router")
    row = next(r for r in rows if r["msg_id"] == result["msg_id"])
    assert row["body"] == {"text": "hello over the real bus"}
    assert row["to"] == "worker"


def test_envelope_is_the_keeper_accepted_canonical_form(bus, monkeypatch):
    """The assertion derives from the keeper-verified canonical form (G2-1).

    The real keeper accepted the post, so its ``Registry.validate_post`` passed
    against ``canonical_post({from, to, ts, nonce, msg_id, kid, text})``.  We
    capture the payload that was accepted and re-derive the signature with the
    same canonical form and the key file the inventory provisions.
    """
    capture = _capture_posts(monkeypatch)
    client = build_client(bus.env())
    result = client.send("worker", "canonical check")
    assert result["queued"] is False
    row = next(r for r in bus.fetch_rows("router")
               if r["msg_id"] == result["msg_id"])
    assert row["body"] == {"text": "canonical check"}
    envelope = capture["payload"]["envelope"]
    signed = {"from": envelope["host"], "to": capture["payload"]["to"],
              "ts": envelope["ts"], "nonce": envelope["nonce"],
              "msg_id": envelope["msg_id"], "kid": "kid-router",
              "text": _wire_text(capture["payload"]["body"]["text"])}
    canonical = contract.canonical_post(signed)
    assert contract.sign(_secret("router"), canonical) == envelope["sig"]
    # and the server-side registry accepts the very same signature
    # (it raises BusError on any mismatch or unknown key)
    bus.registry.validate_post(
        "router", json.dumps(signed, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"),
        envelope["sig"], float(envelope["ts"]), contract.new_state())


def test_read_shows_own_messages_from_real_keeper(bus):
    client = build_client(bus.env())
    result = client.send("l1", "read me back")
    rows = client.read()                      # no args: since=0 default
    assert isinstance(rows, list)
    assert result["msg_id"] in [row["msg_id"] for row in rows]
    row = next(r for r in rows if r["msg_id"] == result["msg_id"])
    assert row["body"] == {"text": "read me back"}


def test_call_tool_send_and_read_hit_real_keeper(bus):
    receipt = json.loads(call_tool("fleet_msg_send",
                                   {"to": "relay", "text": "tool path"},
                                   env=bus.env()))
    assert receipt["queued"] is False
    payload = json.loads(call_tool("fleet_msg_read", {"since": 0},
                                   env=bus.env()))
    assert receipt["msg_id"] in [row["msg_id"] for row in payload["rows"]]


def test_cli_end_to_end_send_then_read(bus):
    sent = run_cli(bus, ["send", "worker", "from the CLI"])
    assert sent.returncode == 0, sent.stderr
    assert "sent " in sent.stdout
    listed = run_cli(bus, ["read"])
    assert listed.returncode == 0, listed.stderr
    assert "from the CLI" in listed.stdout


def test_send_to_unknown_agent_is_refused_and_nothing_queues(bus):
    client = build_client(bus.env())
    with pytest.raises(Refused):
        client.send("not-an-agent", "into the void")
    assert list((bus.root / "spool-router").glob("*.json")) == []


# --------------------------------------------------------------------------
# EXTRA: --json flag serializes JSON to text string; G1-1: body == {"text": <str>}
# --------------------------------------------------------------------------

def test_json_flag_serializes_json_text(bus):
    sent = run_cli(bus, ["send", "worker", '{"a": 1}', "--json"])
    assert sent.returncode == 0, sent.stderr
    row = bus.fetch_rows("router")[-1]
    assert row["body"] == {"text": '{"a":1}'}


def test_json_flag_invalid_json_is_refused(bus):
    sent = run_cli(bus, ["send", "worker", "not json", "--json"])
    assert sent.returncode != 0
    assert "not valid JSON" in sent.stderr
    assert bus.fetch_rows("router") == []


def test_string_body_signs_the_original_raw_text(bus, monkeypatch):
    """A raw-text body signs the raw text; never a JSON re-encoding (G1 rule)."""
    capture = _capture_posts(monkeypatch)
    raw = '{"a": 1}'                       # valid JSON sent WITHOUT --json
    client = build_client(bus.env())
    result = client.send("worker", raw)
    row = next(r for r in bus.fetch_rows("router")
               if r["msg_id"] == result["msg_id"])
    assert row["body"] == {"text": raw}
    envelope = capture["payload"]["envelope"]

    def sig_for(text):
        signed = {"from": envelope["host"], "to": capture["payload"]["to"],
                  "ts": envelope["ts"], "nonce": envelope["nonce"],
                  "msg_id": envelope["msg_id"], "kid": "kid-router",
                  "text": text}
        return contract.sign(_secret("router"), contract.canonical_post(signed))

    assert sig_for(raw) == envelope["sig"]                 # the raw text
    assert sig_for('{"a":1}') != envelope["sig"]           # not re-encoded


def test_envelope_ts_computed_after_drain(bus, monkeypatch):
    """V6-6: the envelope ts is computed after the spool drain."""
    drain_time = None
    original_drain = BusClient.drain

    def instrumented_drain(self):
        nonlocal drain_time
        res = original_drain(self)
        drain_time = int(self.clock())
        return res

    monkeypatch.setattr(BusClient, "drain", instrumented_drain)
    capture = _capture_posts(monkeypatch)
    client = build_client(bus.env())
    result = client.send("worker", "drain test")
    assert result["queued"] is False
    envelope = capture["payload"]["envelope"]
    assert drain_time is not None
    assert envelope["ts"] >= drain_time


def test_retry_with_same_msg_id_is_idempotent(bus):
    """G2-1: retrying with the same msg_id against real keeper succeeds idempotently."""
    client = build_client(bus.env())
    res1 = client.send("worker", "idempotent message")
    assert res1["queued"] is False
    entry = {"msg_id": res1["msg_id"], "host": client.cfg.host,
             "to": "worker", "text": "idempotent message",
             "created": int(client.clock()), "attempts": 1}
    endpoint = client.resolve("worker")
    payload = client._payload(entry)
    res2 = client._attempt(entry, endpoint, payload)
    assert res2["queued"] is False
    assert res2["msg_id"] == res1["msg_id"]
    assert res2["seq"] == res1["seq"]


def test_replay_with_same_nonce_is_refused(bus):
    """G2-1: replaying an envelope with the same nonce is rejected by the real keeper."""
    client = build_client(bus.env())
    entry = {"msg_id": uuid.uuid4().hex, "host": client.cfg.host,
             "to": "worker", "text": "nonce replay test",
             "created": int(client.clock()), "attempts": 0}
    endpoint = client.resolve("worker")
    payload = client._payload(entry)
    res = client._attempt(entry, endpoint, payload)
    assert res["queued"] is False
    with pytest.raises(Refused, match="replayed_nonce"):
        client.transport.request(endpoint.rstrip("/") + "/v1/post_message",
                                 method="POST", payload=payload)


def test_body_cap_refused_against_real_keeper(bus):
    """G2-1: message body exceeding BODY_MAX is refused by the real keeper."""
    client = build_client(bus.env())
    endpoint = client.resolve("worker")
    raw_payload = {
        "from": "router", "to": "worker",
        "body": {"text": "x" * (contract.BODY_MAX + 1)},
    }
    with pytest.raises(Refused, match="too_large"):
        client.transport.request(endpoint.rstrip("/") + "/v1/post_message",
                                 method="POST", payload=raw_payload)


# --------------------------------------------------------------------------
# G2-2: spool durability — fsync around os.replace, quarantine, modes
# --------------------------------------------------------------------------

def _entry(msg_id, to="worker", text="hi"):
    return {"msg_id": msg_id, "host": "router", "to": to, "text": text,
            "body": text, "created": time.time()}


def test_spool_entry_survives_reopen(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.add(_entry("a" * 32))
    again = Spool(tmp_path / "spool")
    queued = again.entries()
    assert [e["msg_id"] for e in queued] == ["a" * 32]


def test_spool_replace_failure_never_leaves_a_partial_entry(
        tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool")
    spool.add(_entry("a" * 32))                # queued first: must survive

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr("plangraph.fleet_bus.client.os.replace", boom)
    with pytest.raises(Refused):
        spool.add(_entry("b" * 32))
    monkeypatch.undo()
    names = {p.name for p in (tmp_path / "spool").glob("*.json")}
    assert names == {"a" * 32 + ".json"}       # no partial b*.json
    assert [e["msg_id"] for e in Spool(tmp_path / "spool").entries()] == ["a" * 32]


def test_spool_corrupt_entry_is_quarantined_never_silently_dropped(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool._mkdir()
    (tmp_path / "spool" / "bad.json").write_text("{not json", encoding="utf-8")
    spool.add(_entry("a" * 32))
    logs = []
    spool = Spool(tmp_path / "spool", log=logs.append)
    assert [e["msg_id"] for e in spool.entries()] == ["a" * 32]
    quarantined = tmp_path / "spool" / "corrupt" / "bad.json"
    assert quarantined.read_text(encoding="utf-8") == "{not json"
    assert any("corrupt" in message for message in logs)


def test_spool_corrupt_created_is_quarantined_and_does_not_crash(tmp_path):
    """V6-8: a corrupt `created` goes to quarantine and does not crash."""
    spool = Spool(tmp_path / "spool")
    spool._mkdir()
    bad_entry = {"msg_id": "b" * 32, "host": "router", "to": "worker",
                 "text": "corrupt created", "created": "yesterday"}
    (tmp_path / "spool" / ("b" * 32 + ".json")).write_text(
        json.dumps(bad_entry), encoding="utf-8")
    spool.add(_entry("a" * 32))
    logs = []
    spool = Spool(tmp_path / "spool", log=logs.append)
    assert [e["msg_id"] for e in spool.entries()] == ["a" * 32]
    quarantined = tmp_path / "spool" / "corrupt" / ("b" * 32 + ".json")
    assert quarantined.exists()
    assert any("corrupt" in message for message in logs)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_spool_file_modes_are_0700_and_0600(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.add(_entry("a" * 32))
    assert stat.S_IMODE((tmp_path / "spool").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (tmp_path / "spool" / ("a" * 32 + ".json")).stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# G2-3: msg_id doubles as a file name — only uuid4 hex may pass
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "z" * 32, "A" * 32,
                                 "a" * 31, "a" * 33, None, 42])
def test_spool_refuses_non_uuid4_hex_ids(tmp_path, bad):
    spool = Spool(tmp_path / "spool")
    with pytest.raises(Refused):
        spool.add(_entry(bad))
    assert list((tmp_path).glob("**/*.json")) == []


def test_spool_remove_refuses_traversal_quietly(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.add(_entry("a" * 32))
    spool.remove("../../etc/passwd")           # never raises, never escapes
    assert [e["msg_id"] for e in spool.entries()] == ["a" * 32]


# --------------------------------------------------------------------------
# V6-7: size caps refuse clearly (text > TEXT_MAX, body > BODY_MAX)
# --------------------------------------------------------------------------

def test_oversize_text_is_refused(bus):
    client = build_client(bus.env())
    with pytest.raises(Refused, match="text is"):
        client.send("worker", "x" * (contract.TEXT_MAX + 1))
    assert bus.fetch_rows("router") == []


def test_oversize_body_is_refused_before_queueing(bus):
    client = build_client(bus.env())
    with pytest.raises(Refused, match="bus cap"):
        client.send("worker", "\U0001F600" * 1500)
    assert bus.fetch_rows("router") == []
    assert list((bus.root / "spool-router").glob("*.json")) == []


# --------------------------------------------------------------------------
# V6-9: _key_path resolves relative names against the config directory
# --------------------------------------------------------------------------

def test_key_path_relative_resolves_against_base():
    assert _key_path("k.key", "/base") == Path("/base/k.key")


def test_key_path_absolute_is_honored():
    assert _key_path("/abs/k.key", "/base") == Path("/abs/k.key")


def test_inventory_key_file_resolves_next_to_inventory(bus):
    cfg = load_config(bus.env())
    assert cfg.key_file == bus.keys / "router.key"


# --------------------------------------------------------------------------
# G2-6: health identity — a present but foreign key_id claim is never healthy
# --------------------------------------------------------------------------

def test_health_claim_missing_is_tolerated():
    """Older keepers publish no claim; identity is unverified, not hostile."""
    transport = FakeTransport({"status": "ok"})
    assert _health("http://peer:1", _cfg(), transport,
                   expected={"router": "kid-router"}) is True


def test_health_claim_matching_is_healthy():
    transport = FakeTransport({"status": "ok", "key_id": "kid-router"})
    assert _health("http://peer:1", _cfg(), transport,
                   expected={"router": "kid-router"}) is True


def test_health_claim_foreign_is_never_healthy():
    transport = FakeTransport({"status": "ok", "key_id": "someone-elses"})
    assert _health("http://peer:1", _cfg(), transport,
                   expected={"router": "kid-router"}) is False


def test_health_foreign_service_without_ok_status_is_never_healthy():
    """A 2xx from another service (e.g. nginx or other API) is not healthy."""
    transport = FakeTransport({"message": "welcome to foreign api"})
    assert _health("http://peer:1", _cfg(), transport,
                   expected={"router": "kid-router"}) is False


def test_health_probe_sends_no_bearer_token(bus, monkeypatch):
    """G2-6: the health probe sends no bearer token."""
    captured_auth = []
    real_open = urllib.request.OpenerDirector.open

    def spy_open(self, req, *args, **kwargs):
        if "/v1/health" in req.full_url:
            captured_auth.append(req.headers.get("Authorization"))
        return real_open(self, req, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy_open)
    cfg = load_config(bus.env())
    # cfg has token_file set, but health probe must not send Authorization header
    healthy = _health(bus.url, cfg, expected={"router": "kid-router"})
    assert healthy is True
    assert len(captured_auth) >= 1
    assert all(h is None for h in captured_auth)


def test_health_claim_map_is_checked_per_host():
    ok = {"key_id": {"router": "kid-router", "worker": "kid-worker"}}
    bad = {"key_id": {"router": "kid-router", "worker": "wrong"}}
    expected = {"router": "kid-router", "worker": "kid-worker"}
    assert _identity_ok(ok, expected) is True
    assert _identity_ok(bad, expected) is False
    assert _identity_ok({"status": "ok"}, expected) is True   # missing: tolerated
    assert _identity_ok(ok, {}) is False                      # nothing expected


def test_health_real_http_claim_gate():
    # one real HTTP round trip per claim shape through a tiny fake keeper
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        claim = None

        def do_GET(self):  # noqa: N802 - stdlib signature
            body = json.dumps({"status": "ok", **(self.claim or {})}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        Handler.claim = {"key_id": "kid-router"}
        assert _health(endpoint, _cfg(), expected={"router": "kid-router"}) is True
        Handler.claim = {"key_id": "not-ours"}
        assert _health(endpoint, _cfg(),
                       expected={"router": "kid-router"}) is False
    finally:
        server.shutdown()
        server.server_close()


def _cfg():
    return Config(inventory=Path("inventory.json"), host="router",
                  key_id="kid-router", key_file=Path("router.key"),
                  spool=Path("spool"), timeout=2.0)


# --------------------------------------------------------------------------
# G2-7: the unauthenticated probe is deadline-bound (~2 s), never a hang
# --------------------------------------------------------------------------

def test_probe_timeout_is_bounded():
    assert PROBE_TIMEOUT_S == 2.0


def test_prober_answers_unreachable_peers_within_budget():
    prober = _Prober(_cfg(), {"http://10.255.255.1:8765": {}})
    started = time.monotonic()
    healthy = prober("http://10.255.255.1:8765")
    elapsed = time.monotonic() - started
    assert healthy is False
    assert elapsed < 4.0, f"probe blocked for {elapsed:.1f}s"


def test_prober_prime_probes_peers_concurrently():
    endpoints = {f"http://10.255.255.{i}:8765": {} for i in (1, 2, 3)}
    prober = _Prober(_cfg(), endpoints)
    started = time.monotonic()
    prober.prime()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"3 peers probed serially: {elapsed:.1f}s"


# --------------------------------------------------------------------------
# G2-8: every successful write is handed to the wake hook exactly once
# --------------------------------------------------------------------------

class CountingWake:
    def __init__(self):
        self.rows = []

    def on_row(self, row):
        self.rows.append(row)


def test_successful_write_reaches_the_wake_hook_once(bus):
    hook = CountingWake()
    client = build_client(bus.env(), wake=hook)
    result = client.send("worker", "wake me")
    assert len(hook.rows) == 1
    assert hook.rows[0]["msg_id"] == result["msg_id"]


def test_two_writes_reach_the_wake_hook_twice(bus):
    hook = CountingWake()
    client = build_client(bus.env(), wake=hook)
    client.send("worker", "one")
    client.send("worker", "two")
    assert len(hook.rows) == 2


def test_unconfigured_wake_logs_once_and_never_per_row(bus):
    logs = []
    client = build_client(bus.env(), log=logs.append)
    client.send("worker", "one")
    client.send("worker", "two")
    wake_logs = [m for m in logs if "wake" in m.lower()]
    assert len(wake_logs) == 1, logs     # one construction, one log, not per row


def test_send_to_configured_target_triggers_sink(bus, monkeypatch):
    """G2-8: send to a configured wake target triggers the sink."""
    from plangraph.fleet_bus import wake_http
    prompts = []

    def fake_post(url, body, timeout, headers):
        prompts.append((url, body))
        return 200, b'{"ok": true}'

    wake_file = bus.root / "wake.json"
    wake_file.write_text(json.dumps({
        "sessions": {
            "worker": {"session": "sess-worker", "base": "http://127.0.0.1:9876", "kind": "opencode"},
        },
        "state": str(bus.root / "wake-state.json"),
    }))
    env = dict(bus.env(host="l1"))
    env["FLEET_MSG_WAKE"] = str(wake_file)

    monkeypatch.setattr(wake_http, "_post", fake_post)
    client = build_client(env)
    assert client.wake is not None
    client.wake.sink.post = fake_post

    res = client.send("worker", "hello worker wake")
    assert res["queued"] is False
    assert len(prompts) == 1
    url, body = prompts[0]
    assert "sess-worker" in url


# --------------------------------------------------------------------------
# G2-4 / G2-5 / V7-3: the shims boot the CLI safely and are really executed
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shims():
    return {"posix": (REPO / "tools" / "fleet-msg").read_text(encoding="utf-8"),
            "cmd": (REPO / "tools" / "fleet-msg.cmd").read_text(encoding="utf-8"),
            "ps1": (REPO / "tools" / "fleet-msg.ps1").read_text(encoding="utf-8")}


def test_shims_bootstrap_python_safely(shims):
    """G2-4: no unsafe bootstrap; -P (or -I, which implies -P) everywhere."""
    assert "python3 -P -m" in shims["posix"]
    assert re.search(r"python3? -I ", shims["cmd"])      # -I implies -P (3.11+)
    assert "-P" in shims["ps1"]
    assert re.search(r"py(thon)?\d? -E\b", shims["ps1"]) is None


def test_posix_shim_runs_for_real(bus):
    """V7-3: the POSIX shim is executed, not just parsed."""
    env = dict(os.environ)
    env.update(bus.env())
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.run(["bash", str(REPO / "tools" / "fleet-msg"), "--help"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()


def test_cmd_shim_never_expands_argv_onto_a_command_line(shims):
    """G2-5: %* is captured into the environment once; a fixed command line
    splits it with CommandLineToArgvW.  Quoting contract and the unbalanced
    double-quote limitation are documented."""
    cmd = shims["cmd"]
    assert 'set "FLEET_MSG_ARGV=%*"' in cmd        # env capture, never a cmdline
    assert "CommandLineToArgvW" in cmd             # faithful re-splitting
    code_lines = [line for line in cmd.splitlines()
                  if not line.lstrip().lower().startswith(("rem", "::"))]
    assert all("FLEET_MSG_ARGV" in line for line in code_lines if "%*" in line)
    lowered = cmd.lower()
    assert "quote" in lowered or "quoting" in lowered
    assert "unbalanced" in lowered or "use the powershell" in lowered \
        or ".ps1" in cmd                            # documented escape hatch


@pytest.mark.skipif(sys.platform != "win32",
                    reason="cmd.exe only exists on Windows; "
                           "parse-level contract asserted on every platform")
def test_cmd_shim_executes_on_windows(bus):
    proc = subprocess.run(
        ["cmd", "/c", str(REPO / "tools" / "fleet-msg.cmd"), "--help"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()


def test_ps1_shim_parses_or_skips_with_reason(bus):
    """V7-3: the PowerShell shim is parse-checked where a shell exists."""
    pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe") \
        or shutil.which("powershell.exe")
    if not pwsh:
        assert "@args" in (REPO / "tools" / "fleet-msg.ps1").read_text(
            encoding="utf-8")                      # static contract still holds
        pytest.skip("pwsh not installed in this environment; "
                    "static shim contract asserted instead")
    script = str(REPO / "tools" / "fleet-msg.ps1")
    if pwsh.endswith(".exe"):                      # Windows interop: native path
        script = subprocess.run(["wslpath", "-w", script],
                                capture_output=True, text=True).stdout.strip()
    command = ("$errors = $null; "
               "$null = [System.Management.Automation.Language.Parser]"
               "::ParseFile(" + json.dumps(script) + ", [ref]$null, "
               "[ref]$errors); if ($errors.Count) { exit 1 }")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command",
                           command],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
