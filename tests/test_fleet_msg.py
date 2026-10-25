"""BUS-ANY-AGENT-CLI: ``fleet-msg`` is the client half of the build-L1 bus.

Each DONE behaviour and each attack vector has its own test:

* ``send`` resolves the agent through ``fleet_bus.discovery.Registry.resolve``
  and posts a signed write; the receipt is fed to a real ``WakeHook.on_row``,
* an unknown agent is refused before any request and before anything queues,
* text and body caps refuse before any request,
* every write is signed from the host key *file* (path from config), key
  material never appears in env, argv, requests, the spool or output,
* the local spool retries the **same** ``msg_id``; the keeper stays idempotent,
* replayed nonce / bad signature are refused by the keeper's contract state and
  never retried forever,
* ``read`` accepts ``--since <seq|time>``; a missing wake hook logs and no-ops,
* the MCP pair ``fleet_msg_send`` / ``fleet_msg_read`` registers and round-trips,
* the shims (``tools/fleet-msg``, ``.cmd``, ``.ps1``) stay thin: no ``fcntl``,
  no keeper store, no key material.

The keeper here is a FAKE that implements the documented contract: envelope
verification via ``Registry.validate_post`` (reusing the host key file and the
fleet-bus v2 replay/dedupe state), idempotent ``msg_id`` and replay refusal.
No keeper code is edited by this suite.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import plangraph.keeper.mcp as mcp
from plangraph.fleet_bus import client as bus
from plangraph.fleet_bus.contract import (
    BODY_MAX,
    ERROR_STATUS,
    TEXT_MAX,
    BusError,
    canonical_post,
    new_state,
    sign,
)
from plangraph.fleet_bus.wake import SessionTarget, WakeHook, WakeState

TOKEN = "fm-test-token"
REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "fleet-msg"
CMD_SHIM = REPO / "tools" / "fleet-msg.cmd"
PS1_SHIM = REPO / "tools" / "fleet-msg.ps1"
EPOCH = 1_700_000_000.0


class _Handler(BaseHTTPRequestHandler):
    """Fake keeper: contract-only (envelope, idempotency, replay), no store."""

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        header = self.headers.get("Authorization", "")
        return header == "Bearer " + self.server.token

    def do_GET(self):
        srv = self.server
        srv.calls.append({"method": "GET", "path": self.path,
                          "headers": dict(self.headers)})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/v1/health":
            return self._json(200, {"ok": True})
        if not self._auth():
            return self._json(401, {"error": "unauthorized", "msg": "bearer"})
        if parsed.path == "/v1/messages":
            query = urllib.parse.parse_qs(parsed.query)
            rows = list(srv.rows)
            if "since" in query:
                rows = [row for row in rows if row["seq"] > int(query["since"][0])]
            if "ts_min" in query:
                rows = [row for row in rows if row["ts"] >= int(query["ts_min"][0])]
            return self._json(200, {"rows": rows})
        return self._json(404, {"error": "not_found", "msg": parsed.path})

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        srv.calls.append({"method": "POST", "path": self.path,
                          "headers": dict(self.headers),
                          "body": raw.decode("utf-8", "replace")})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/v1/post_message":
            return self._json(404, {"error": "not_found", "msg": parsed.path})
        if not self._auth():
            return self._json(401, {"error": "unauthorized", "msg": "bearer"})
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return self._json(400, {"error": "bad_request", "msg": "not json"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "bad_request", "msg": "body"})
        envelope = payload.get("envelope")
        if not isinstance(envelope, dict) or \
                set(envelope) != {"host", "sig", "nonce", "ts", "msg_id"}:
            return self._json(400, {"error": "bad_request",
                                    "msg": "envelope is {host,sig,nonce,ts,msg_id}"})
        srv.msg_ids.append(envelope["msg_id"])
        if srv.fail_next:
            srv.fail_next -= 1
            return self._json(503, {"error": "unavailable", "msg": "injected"})
        host = envelope["host"]
        if payload.get("from") != host:
            return self._json(400, {"error": "bad_request",
                                    "msg": "envelope host must match from"})
        body = payload.get("body")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return self._json(400, {"error": "bad_request", "msg": "body.text"})
        record = srv.registry.hosts.get(host)
        inner = {"from": host, "to": payload.get("to"), "ts": envelope["ts"],
                 "nonce": envelope["nonce"], "msg_id": envelope["msg_id"],
                 "kid": record.key_id if record else "", "text": text}
        encoded = json.dumps(inner, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        try:
            ack = srv.registry.validate_post(host, encoded, envelope["sig"],
                                             srv.clock(), srv.state)
        except BusError as exc:
            return self._json(ERROR_STATUS.get(exc.code, 400),
                              {"error": exc.code, "msg": str(exc)})
        if ack.get("dup"):
            for row in srv.rows:
                if row.get("msg_id") == envelope["msg_id"]:
                    return self._json(200, {"row": row, "ack": ack})
            return self._json(500, {"error": "unavailable", "msg": "dup"})
        row = {"seq": srv.rows[-1]["seq"] + 1 if srv.rows else 1,
               "from": host, "to": payload.get("to"),
               "ts": int(srv.clock() * 1000), "body": body, "key": "",
               "trace_id": "", "msg_id": envelope["msg_id"],
               "ack_state": "pending", "attempt_n": 0}
        srv.rows.append(row)
        return self._json(200, {"row": row, "ack": ack})


@pytest.fixture
def keeper(tmp_path):
    """Fake keeper plus a provisioned inventory, status snapshot and env."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.calls, server.rows, server.msg_ids = [], [], []
    server.state, server.fail_next, server.token = new_state(), 0, TOKEN
    server.clock = time.time
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    key_path = tmp_path / "alpha.key"
    key_path.write_bytes(b"K" * 48)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "port": port,
        "hosts": [
            {"host": "alpha", "node_id": "node-a", "key_id": "ka",
             "key_file": str(key_path)},
            {"host": "beta", "node_id": "node-b", "key_id": "kb",
             "key_file": str(tmp_path / "beta.key")},
        ],
        "agents": {"worker": "beta"},
    }, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "beta.key").write_bytes(b"B" * 48)
    # MagicDNS data is routing only: the endpoint must reach the fake keeper.
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"Peer": {
        "node-a": {"HostName": "alpha", "DNSName": "127.0.0.1",
                   "Online": True, "OS": "linux"},
        "node-b": {"HostName": "beta", "DNSName": "127.0.0.1",
                   "Online": True, "OS": "linux"},
    }}), encoding="utf-8")
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    env = {"FLEET_MSG_INVENTORY": str(inventory),
           "FLEET_MSG_HOST": "alpha",
           "FLEET_MSG_SPOOL": str(tmp_path / "spool"),
           "FLEET_MSG_STATUS_FILE": str(status),
           "PG_TOKEN_FILE": str(token_file)}
    base = f"http://127.0.0.1:{port}"
    server.registry = bus.load_registry(bus.load_config(env))
    yield SimpleNamespace(server=server, env=env, base=base, tmp=tmp_path,
                          key_path=key_path, inventory=inventory)
    server.shutdown()
    server.server_close()


def build(keeper, **kw):
    """A client over the fake keeper: real discovery, transport, probe, spool."""
    return bus.build_client(env=dict(keeper.env), **kw)


def posts(keeper):
    return [call for call in keeper.server.calls if call["method"] == "POST"]


def spooled(keeper):
    return bus.Spool(keeper.env["FLEET_MSG_SPOOL"]).entries()


# --------------------------------------------------------------- configuration

def test_missing_inventory_is_a_clear_refusal():
    with pytest.raises(bus.Refused) as exc:
        bus.build_client(env={"FLEET_MSG_HOST": "alpha"})
    assert "FLEET_MSG_INVENTORY" in str(exc.value)


def test_key_material_in_the_environment_is_refused(keeper):
    env = dict(keeper.env, FLEET_MSG_KEY="raw-key-bytes-never-in-env")
    with pytest.raises(bus.Refused) as exc:
        bus.build_client(env=env)
    assert "environment" in str(exc.value)
    assert posts(keeper) == []


def test_host_missing_from_the_inventory_is_refused(keeper):
    with pytest.raises(bus.Refused) as exc:
        bus.build_client(env=dict(keeper.env, FLEET_MSG_HOST="gamma"))
    assert "gamma" in str(exc.value)


# ------------------------------------------------------------- agent addressing

def test_unknown_agent_is_refused_before_any_request(keeper):
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("ghost", "hello")
    assert "ghost" in str(exc.value)
    assert posts(keeper) == []
    assert spooled(keeper) == []


def test_offline_agent_is_refused_before_any_request(keeper):
    """Resolution needs a healthy endpoint: the snapshot says beta is gone."""
    status_path = Path(keeper.env["FLEET_MSG_STATUS_FILE"])
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["Peer"]["node-b"]["Online"] = False
    status_path.write_text(json.dumps(status), encoding="utf-8")
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "hello")
    assert "worker" in str(exc.value)
    assert posts(keeper) == []
    assert spooled(keeper) == []


def test_cli_unknown_agent_exits_2_with_a_refusal(keeper, capsys):
    client = build(keeper)
    assert bus.main(["send", "ghost", "hi"], client=client) == 2
    captured = capsys.readouterr()
    assert "refused" in captured.err and "ghost" in captured.err
    assert posts(keeper) == []


# ------------------------------------------------------------------ size caps

def test_text_over_cap_is_refused_before_any_request(keeper):
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "x" * (TEXT_MAX + 1))
    assert str(TEXT_MAX) in str(exc.value)
    assert posts(keeper) == []
    assert spooled(keeper) == []


def test_text_at_the_cap_is_accepted(keeper):
    client = build(keeper)
    result = client.send("worker", "x" * TEXT_MAX)
    assert result["queued"] is False and result["seq"] == 1
    assert result["row"]["from"] == "alpha" and result["row"]["to"] == "worker"


def test_body_cap_is_enforced_before_any_request(keeper, monkeypatch):
    monkeypatch.setattr(bus, "BODY_MAX", 512)
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "y" * 400)
    assert "512" in str(exc.value)
    assert posts(keeper) == []
    assert spooled(keeper) == []


# ---------------------------------------------------------- host key signing

class _Sink:
    """Recording wake sink: no sockets, like the wake suite's sink."""

    def __init__(self):
        self.calls = []

    def wake(self, target, text):
        self.calls.append((target.session, text))


def test_send_resolves_signs_and_wakes_through_a_real_hook(keeper):
    sink = _Sink()
    hook = WakeHook({"worker": SessionTarget("ses_w1", "http://sink.example.test:4096")},
                    sink, WakeState(path=keeper.tmp / "wake.json"),
                    log=lambda message: None)
    client = build(keeper, wake=hook)
    result = client.send("worker", "status report")
    assert result["queued"] is False
    assert type(client.wake) is WakeHook          # the real seam, not a stand-in
    assert hook.cursor == result["seq"] == 1      # the row really was accounted
    assert sink.calls == [("ses_w1", "fleet-wake")]
    assert posts(keeper)[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert keeper.server.rows[0]["from"] == "alpha"
    assert keeper.server.rows[0]["to"] == "worker"


def test_every_write_is_signed_from_the_host_key_file(keeper):
    client = build(keeper)
    client.send("worker", "one")
    call = posts(keeper)[0]
    payload = json.loads(call["body"])
    envelope = payload["envelope"]
    inner = {"from": "alpha", "to": payload["to"], "ts": envelope["ts"],
             "nonce": envelope["nonce"], "msg_id": envelope["msg_id"],
             "kid": "ka", "text": payload["body"]["text"]}
    assert envelope["sig"] == sign(keeper.key_path.read_bytes(),
                                   canonical_post(inner))
    secret = keeper.key_path.read_text(encoding="utf-8")
    blob = call["body"] + json.dumps(call["headers"])
    assert secret not in blob


def test_the_key_file_is_reread_for_every_write(keeper):
    client = build(keeper)
    client.send("worker", "first")
    keeper.key_path.write_bytes(b"R" * 48)          # rotate: same path, new key
    client.send("worker", "second")
    sigs = [json.loads(call["body"])["envelope"]["sig"]
            for call in posts(keeper)]
    assert len(sigs) == 2 and sigs[0] != sigs[1]
    assert len(keeper.server.rows) == 2             # both verified by the file


def test_key_material_never_reaches_config_requests_spool_or_output(keeper,
                                                                   monkeypatch,
                                                                   capsys):
    monkeypatch.setenv("FLEET_MSG_DECOY", "decoy-key-material-000")
    client = build(keeper)
    secret = keeper.key_path.read_text(encoding="utf-8")
    cfg = bus.load_config(keeper.env)
    assert secret not in json.dumps(
        {name: str(value) for name, value in vars(cfg).items()})
    client.send("worker", "quiet write")
    client.read(0)
    captured = capsys.readouterr()
    spool_text = "".join(path.read_text(encoding="utf-8")
                         for path in Path(keeper.env["FLEET_MSG_SPOOL"]).glob("*"))
    calls = json.dumps(keeper.server.calls, sort_keys=True)
    assert secret not in calls + captured.out + captured.err + spool_text
    assert "decoy-key-material-000" not in calls + spool_text


@pytest.mark.parametrize("damage", ["missing", "short"])
def test_an_unusable_key_file_is_refused_before_any_request(keeper, damage):
    if damage == "missing":
        keeper.key_path.unlink()
    else:
        keeper.key_path.write_bytes(b"short")
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "hi")
    assert "key" in str(exc.value)
    assert posts(keeper) == []
    assert spooled(keeper) == []


# ---------------------------------------------------------------- the spool

def test_the_spool_retries_the_same_msg_id_and_the_keeper_stays_idempotent(keeper):
    client = build(keeper)
    keeper.server.fail_next = 1                      # one injected 503
    first = client.send("worker", "flaky write")
    assert first["queued"] is True
    assert [entry["msg_id"] for entry in spooled(keeper)] == [first["msg_id"]]
    retry = client.drain()
    assert retry and retry[0]["queued"] is False and retry[0]["seq"] == 1
    assert keeper.server.msg_ids == [first["msg_id"], first["msg_id"]]
    assert len(keeper.server.rows) == 1              # idempotent: one row
    assert spooled(keeper) == []


def test_the_spool_survives_a_client_restart_with_the_same_msg_id(keeper):
    client = build(keeper)
    keeper.server.fail_next = 1
    queued = client.send("worker", "written later")
    restarted = build(keeper)                        # fresh objects, same spool
    retry = restarted.drain()
    assert retry[0]["msg_id"] == queued["msg_id"]
    assert len(keeper.server.rows) == 1
    assert keeper.server.msg_ids == [queued["msg_id"], queued["msg_id"]]
    assert spooled(keeper) == []


def test_a_second_cli_send_drains_the_backlog_first(keeper, capsys):
    client = build(keeper)
    keeper.server.fail_next = 1
    assert bus.main(["send", "worker", "first"], client=client) == 0
    assert "queued" in capsys.readouterr().out
    assert bus.main(["send", "worker", "second"], client=client) == 0
    assert "sent" in capsys.readouterr().out
    assert len(keeper.server.rows) == 2
    assert len(set(keeper.server.msg_ids)) == 2      # 3 attempts, 2 messages


def test_a_replayed_nonce_is_refused_by_the_keeper_and_never_retried(keeper):
    client = build(keeper)
    client._nonce = lambda: "fixed-nonce-0000000000000000000000000000"  # reuse
    client.send("worker", "once")
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "again")
    assert "replayed_nonce" in str(exc.value)
    assert spooled(keeper) == []                     # permanent: no backlog
    assert len(keeper.server.rows) == 1              # nothing appended


def test_a_forged_signature_is_refused_by_the_keeper_and_never_queued(keeper,
                                                                     monkeypatch):
    monkeypatch.setattr(bus, "sign", lambda secret, canonical: "0" * 64)
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.send("worker", "forged")
    assert "bad_signature" in str(exc.value)
    assert keeper.server.rows == []
    assert spooled(keeper) == []


# -------------------------------------------------------------------- reading

def test_read_accepts_a_sequence_cursor_and_a_timestamp(keeper):
    client = build(keeper)
    client.send("worker", "one")
    client.send("worker", "two")
    rows = keeper.server.rows
    assert [row["seq"] for row in client.read(0)] == [1, 2]
    assert [row["seq"] for row in client.read(1)] == [2]
    iso = datetime.fromtimestamp(rows[0]["ts"] / 1000, tz=timezone.utc).isoformat()
    assert [row["seq"] for row in client.read(iso)] == [1, 2]
    assert [row["seq"] for row in client.read(f"{rows[0]['ts'] / 1000:.3f}")] == [1, 2]
    later = datetime.fromtimestamp(rows[0]["ts"] / 1000 + 30,
                                   tz=timezone.utc).isoformat()
    assert client.read(later) == []


def test_read_refuses_a_cursor_it_cannot_parse(keeper):
    client = build(keeper)
    with pytest.raises(bus.Refused) as exc:
        client.read("yesterday")
    assert "yesterday" in str(exc.value)


def test_cli_read_prints_rows_and_rejects_a_bad_cursor(keeper, capsys):
    client = build(keeper)
    assert bus.main(["send", "worker", "hello"], client=client) == 0
    capsys.readouterr()
    assert bus.main(["read", "--since", "0"], client=client) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert rows[0]["body"] == {"text": "hello"}
    assert bus.main(["read", "--since", "yesterday"], client=client) == 2
    captured = capsys.readouterr()
    assert "refused" in captured.err and "yesterday" in captured.err


# ---------------------------------------------------------------- wake, wired

def test_a_missing_wake_hook_is_a_logged_no_op(keeper):
    lines = []
    client = build(keeper, log=lines.append)
    result = client.send("worker", "no hook configured")
    assert result["queued"] is False
    assert any("wake" in line for line in lines)


# ------------------------------------------------------- the MCP tool pair

def use_env(keeper, monkeypatch):
    for name, value in keeper.env.items():
        monkeypatch.setenv(name, value)


def rpc(name, arguments):
    return mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}, None)


def test_mcp_registers_the_bus_tool_pair():
    names = [tool["name"] for tool in mcp.TOOLS]
    assert "fleet_msg_send" in names and "fleet_msg_read" in names
    send = next(tool for tool in mcp.TOOLS if tool["name"] == "fleet_msg_send")
    assert set(send["inputSchema"]["required"]) == {"to", "text"}
    read = next(tool for tool in mcp.TOOLS if tool["name"] == "fleet_msg_read")
    assert "since" in read["inputSchema"]["properties"]


def test_mcp_send_is_signed_and_read_returns_rows(keeper, monkeypatch):
    use_env(keeper, monkeypatch)
    reply = rpc("fleet_msg_send", {"to": "worker", "text": "via mcp"})
    assert reply["result"]["isError"] is False
    assert keeper.server.rows[0]["body"] == {"text": "via mcp"}
    envelope = json.loads(posts(keeper)[0]["body"])["envelope"]
    assert set(envelope) == {"host", "sig", "nonce", "ts", "msg_id"}
    reply = rpc("fleet_msg_read", {"since": "0"})
    assert reply["result"]["isError"] is False
    rows = json.loads(reply["result"]["content"][0]["text"])["rows"]
    assert [row["seq"] for row in rows] == [1]


def test_mcp_unknown_agent_is_a_tool_error(keeper, monkeypatch):
    use_env(keeper, monkeypatch)
    reply = rpc("fleet_msg_send", {"to": "ghost", "text": "nowhere"})
    assert reply["result"]["isError"] is True
    assert "ghost" in reply["result"]["content"][0]["text"]
    assert posts(keeper) == []


def test_mcp_without_bus_config_is_a_tool_error(monkeypatch):
    for name in ("FLEET_MSG_INVENTORY", "FLEET_MSG_HOST", "FLEET_MSG_SPOOL",
                 "FLEET_MSG_STATUS_FILE", "PG_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    reply = rpc("fleet_msg_send", {"to": "worker", "text": "unconfigured"})
    assert reply["result"]["isError"] is True
    assert "FLEET_MSG_INVENTORY" in reply["result"]["content"][0]["text"]


def test_mcp_output_never_carries_key_material(keeper, monkeypatch):
    use_env(keeper, monkeypatch)
    reply = rpc("fleet_msg_send", {"to": "worker", "text": "quiet"})
    secret = keeper.key_path.read_text(encoding="utf-8")
    assert secret not in json.dumps(reply)


# --------------------------------------------------- the shims (Linux/Windows)

def test_posix_script_has_a_shebang_exec_bit_and_no_keeper_internals():
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!")
    assert b"\r" not in raw                      # LF only
    assert stat.S_IMODE(SCRIPT.stat().st_mode) & 0o111
    assert b"-m plangraph.fleet_bus.client" in raw
    assert b"fcntl" not in raw and b"store" not in raw
    assert b"key" not in raw                # the shim carries no key path


def test_posix_script_send_and_read_round_trip(keeper):
    env = dict(os.environ, **keeper.env,
               PYTHONPATH=str(REPO / "src"))
    sent = subprocess.run([str(SCRIPT), "send", "worker", "from the shell"],
                          env=env, capture_output=True, text=True, timeout=60)
    assert sent.returncode == 0, sent.stderr
    assert "sent" in sent.stdout
    got = subprocess.run([str(SCRIPT), "read", "--since", "0"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    rows = json.loads(got.stdout)["rows"]
    assert rows[0]["body"] == {"text": "from the shell"}
    secret = keeper.key_path.read_text(encoding="utf-8")
    assert secret not in sent.stdout + sent.stderr + got.stdout + got.stderr


def test_windows_shims_stay_thin_and_forward_every_argument():
    cmd = CMD_SHIM.read_text(encoding="utf-8")
    ps1 = PS1_SHIM.read_text(encoding="utf-8")
    for text in (cmd, ps1):
        assert "-m plangraph.fleet_bus.client" in text
        assert "fcntl" not in text and "keeper" not in text
        assert ".key" not in text and "hmac" not in text
    assert "%*" in cmd and "python" in cmd.lower()
    assert "@args" in ps1 and "python" in ps1.lower()


def test_client_imports_and_serves_help_without_fcntl():
    """Windows has no ``fcntl``; the client (and both shims) must not need it."""
    src = str(REPO / "src")
    prelude = ("import sys, runpy\n"
               "sys.modules['fcntl'] = None\n"
               f"sys.path.insert(0, {src!r})\n")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", prelude +
         "import plangraph.fleet_bus.client as c\n"
         "assert callable(c.main)\n"
         "assert 'plangraph.keeper.store' not in sys.modules, 'store loaded'\n"
         "print('ok')\n"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
    help_run = subprocess.run(
        [sys.executable, "-I", "-c", prelude +
         "sys.argv = ['fleet-msg', '--help']\n"
         "runpy.run_module('plangraph.fleet_bus.client', run_name='__main__')\n"],
        capture_output=True, text=True, timeout=60)
    assert help_run.returncode == 0, help_run.stderr
    assert "fleet-msg" in help_run.stdout
