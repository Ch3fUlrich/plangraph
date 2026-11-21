"""M2B-RUN-TOKENS through the HTTP API, the pg CLI and the MCP route table."""
import json

import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed
from plangraph.keeper import api_routes, mcp
from plangraph.keeper.api import KeeperHandler  # noqa: F401 - import smoke
from test_keeper_run_tokens import FakeRuns


@pytest.fixture
def d(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    world.store.ctx = {"runs": FakeRuns()}
    doors = Doors(world, tmp_path)
    yield doors
    doors.close()
    world.close()


def body(key="demo:A", role="writer", run="w1"):
    return {"key": key, "role": role, "run_id": run}


def test_http_mint_and_use(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    status, out = d.http("l1main", "POST", "/v1/mint_run_token", body())
    assert status == 200 and out["token"] and out["epoch"] == epoch
    w.tokens["runw"] = out["token"]
    assert d.http("runw", "POST", "/v1/heartbeat",
                  {"key": "demo:A", "epoch": epoch})[0] == 200
    snap = d.server.config.snapshot
    import os
    d.server.flush_snapshot()                         # the debounced file exists before it is removed
    os.unlink(snap)
    assert d.http("runw", "POST", "/v1/add_evidence",
                  {"key": "demo:A", "epoch": epoch, "kind": "decision",
                   "ref": {"id": "D-1"}})[0] == 200
    d.server.flush_snapshot()
    assert os.path.exists(snap)                      # snapshot still refreshed
    assert d.http("runw", "POST", "/v1/submit",
                  {"key": "demo:A", "epoch": epoch, "source_round": 0})[0] == 200
    # replay after submit: the revoked token no longer authenticates at all
    status, err = d.http("runw", "POST", "/v1/submit",
                         {"key": "demo:A", "epoch": epoch})
    assert status == 401 and err["error"] == "forbidden"


def test_http_run_token_is_403_everywhere_else(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    w.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token",
                              body())[1]["token"]
    for verb in sorted(api_routes.READ_VERBS - {"write_context"}):
        status, _ = d.http("runw", "GET", f"/v1/{verb}?key=demo:A")
        assert status == 403, verb
    for verb in sorted(api_routes.WRITE_VERBS - {"heartbeat", "add_evidence",
                                                  "submit", "replay", "write_receipt"}):  # replay: M2B-OUTBOX
        status, _ = d.http("runw", "POST", f"/v1/{verb}",
                           {"key": "demo:A", "epoch": epoch, "role": "writer",
                            "run_id": "w1", "ok": True})  # malformed or not: 403
        assert status == 403, verb


def test_http_mint_authorisation(d):
    to_claimed(d.world, "demo:A")
    for who in ("workerA", "reviewer", "l1other", "reader"):
        assert d.http(who, "POST", "/v1/mint_run_token", body())[0] == 403
    assert d.http("router", "POST", "/v1/mint_run_token", body())[0] == 200
    assert d.http("l1main", "POST", "/v1/mint_run_token",
                  body(run="nope"))[0] == 400
    assert d.http("l1main", "POST", "/v1/mint_run_token", {"key": "demo:A"})[0] == 400


@pytest.fixture
def native_client(d, monkeypatch):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    from _spawnd import built_rust_binary

    binary = built_rust_binary("plangraph")
    if not binary.is_file():
        manifest = Path(__file__).resolve().parents[1] / "rust/Cargo.toml"
        subprocess.run(["cargo", "build", "--manifest-path", str(manifest),
                        "-p", "plangraph-cli", "--bin", "plangraph"], check=True)
    assert shutil.which(binary.name, path=str(binary.parent))
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PG_OUTBOX", str(d.tmp / "outbox.jsonl"))
    return d


def test_cli_token_stdout_only_and_env_epoch(native_client, monkeypatch):
    d = native_client
    w = d.world
    epoch = to_claimed(w, "demo:A")
    code, out, err = d.pg("l1main", "token", "demo:A", "--role", "writer",
                          "--run", "w1")
    assert code == 0
    token = out.strip()
    assert token.startswith("pgr1_") and "\n" not in token
    assert token not in err and "pgr1_" not in err
    assert d.pg("workerA", "token", "demo:A", "--role", "writer",
                "--run", "w1")[0] == 2
    w.tokens["runw"] = token
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    assert d.pg("runw", "heartbeat", "demo:A")[0] == 0
    assert d.pg("runw", "whoami")[0] == 2
    files = list((d.tmp / "outbox.jsonl.d" / "done").glob("*.json"))
    assert len(files) == 1 and token not in files[0].read_text()
    if __import__("os").name != "nt":
        assert files[0].stat().st_mode & 0o777 == 0o600


def test_native_replay_recovers_submit_after_socket_loses_reply(native_client, monkeypatch):
    import socket

    d = native_client
    epoch = to_claimed(d.world, "demo:A")
    d.world.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token", body())[1]["token"]
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    assert d.pg("runw", "heartbeat", "demo:A")[0] == 0
    d.world.store.add_evidence(d.world.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    handler = d.server.RequestHandlerClass
    send_json = handler._json
    dropped = []

    def drop_first_submit(self, status, payload):
        if self.path == "/v1/write_receipt" and isinstance(payload, dict) \
                and payload.get("reply", {}).get("state") == "in_review" and not dropped:
            dropped.append(payload["id"])
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        return send_json(self, status, payload)

    monkeypatch.setattr(handler, "_json", drop_first_submit)
    assert d.pg("runw", "submit", "demo:A")[0] == 4
    assert len(dropped) == 1
    status, output, _ = d.pg("runw", "replay")
    assert status == 0 and output.strip() == f"{dropped[0]} duplicate"
    assert d.world.conn.execute("SELECT COUNT(*) FROM events WHERE type='submitted'").fetchone()[0] == 1


def test_old_round_submit_is_refused_on_live_and_receipt_routes(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    w.store.verdict(w.p("reviewer"), "demo:A", False, "review changes")
    minted = d.http("l1main", "POST", "/v1/mint_run_token", body(run="w2"))[1]
    w.tokens["runw"] = minted["token"]
    old = {"key": "demo:A", "epoch": epoch, "source_round": 0}
    assert d.http("runw", "POST", "/v1/submit", old)[0] == 409
    submit = {"id": "01" * 16, "verb": "submit", "body": old}
    assert d.http("runw", "POST", "/v1/write_receipt", submit)[1]["result"] == "round_stale"
    assert d.http("runw", "POST", "/v1/write_receipt", {
        **submit, "body": {**old, "source_round": 1}})[0] == 409
    assert d.http("runw", "POST", "/v1/write_receipt", {
        **submit, "id": "02" * 16, "body": {**old, "source_round": 1}})[1]["result"] == "ok"


def test_mcp_tool_routes():
    names = {t["name"] for t in mcp.TOOLS}
    assert "mint_run_token" in names
    assert mcp._ROUTES["mint_run_token"] == ("POST", "mint_run_token")
