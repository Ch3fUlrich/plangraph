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
    os.unlink(snap)
    assert d.http("runw", "POST", "/v1/add_evidence",
                  {"key": "demo:A", "epoch": epoch, "kind": "decision",
                   "ref": {"id": "D-1"}})[0] == 200
    assert os.path.exists(snap)                      # snapshot still refreshed
    assert d.http("runw", "POST", "/v1/submit",
                  {"key": "demo:A", "epoch": epoch})[0] == 200
    # replay after submit: the revoked token no longer authenticates at all
    status, err = d.http("runw", "POST", "/v1/submit",
                         {"key": "demo:A", "epoch": epoch})
    assert status == 401 and err["error"] == "forbidden"


def test_http_run_token_is_403_everywhere_else(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    w.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token",
                              body())[1]["token"]
    for verb in sorted(api_routes.READ_VERBS):
        status, _ = d.http("runw", "GET", f"/v1/{verb}?key=demo:A")
        assert status == 403, verb
    for verb in sorted(api_routes.WRITE_VERBS - {"heartbeat", "add_evidence",
                                                  "submit", "replay"}):  # replay: M2B-OUTBOX
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


def test_cli_token_stdout_only_and_env_epoch(d, monkeypatch):
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


def test_mcp_tool_routes():
    names = {t["name"] for t in mcp.TOOLS}
    assert "mint_run_token" in names
    assert mcp._ROUTES["mint_run_token"] == ("POST", "mint_run_token")
