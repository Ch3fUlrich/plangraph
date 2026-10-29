"""KEEPER-FLOOR-LEGACY-CLAIM A2 slice 2 (D-3066): the ``waive_floor`` doors.

Slice 1 added the router-only store verb; this slice exposes it through the
same doors ``set_legacy_state`` uses — HTTP ``POST /v1/waive_floor``, the MCP
``waive_floor`` tool and the CLI ``waive-floor`` subcommand — and pins that no
other door grows it (``apply_ops`` first).  The role gate is the store verb's
own ``_require_role(principal, "router")``: no door may bypass it.

Helpers are reused from the slice-1 and legacy-state test modules; nothing
here touches git, the network or the real clock.
"""
from __future__ import annotations

import json
import threading

import pytest

from evidence_fakes import SHA, make_ctx
from keeper_util import World, seed
from plangraph.keeper import api, config, mcp
from plangraph.keeper.store import Store
from test_keeper_evidence_wiring import RecGit
from test_keeper_floor_waive import FLOOR_A, legacy_row
from test_keeper_legacy_state import _api_server, _call, _FakeClient, _mcp_call

BAD_DECISIONS = ["3066", "d-3066", "D-x", "D-3066x", ""]


def _legacy_api(tmp_path):
    """A world whose store has real verifiers, with one legacy row, behind HTTP."""
    world = World(tmp_path)
    world.store = Store(world.conn, clock=world.clock, ctx=make_ctx())
    (key,) = seed(world, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(world, key)
    srv, thread = _api_server(tmp_path, world)
    return world, srv, thread, key, epoch


def _shutdown(world, srv, thread):
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    world.close()


def _floor_events(world):
    return world.conn.execute(
        "SELECT payload FROM events WHERE type='floor_waived'").fetchall()


# ---------------------------------------------------------------------- HTTP
def test_http_waive_floor_router_waives_then_commit_ci_verifies(tmp_path):
    world, srv, thread, key, epoch = _legacy_api(tmp_path)
    try:
        status, body = _call(srv, "/v1/waive_floor",
                             body={"key": key, "decision": "D-3066"},
                             token=world.tok("router"))
        assert status == 200, body
        assert body == {"key": key, "decision": "D-3066", "sha_floor": None}

        rows = _floor_events(world)
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"decision": "D-3066",
                                                 "sha_floor": None,
                                                 "epoch": epoch}

        # The waived legacy row now verifies on the same commit+ci evidence.
        world.store.ctx = make_ctx(git=RecGit(trailers={"Plan-Item": key}))
        out = world.store.add_evidence(world.p("workerA"), key, epoch,
                                       "commit+ci", {"commit": SHA})
        assert out["verified"] is True
    finally:
        _shutdown(world, srv, thread)


def test_http_waive_floor_with_sha_floor_is_recorded(tmp_path):
    world, srv, thread, key, epoch = _legacy_api(tmp_path)
    try:
        status, body = _call(srv, "/v1/waive_floor",
                             body={"key": key, "decision": "D-3066",
                                   "sha_floor": FLOOR_A},
                             token=world.tok("router"))
        assert status == 200, body
        assert body == {"key": key, "decision": "D-3066", "sha_floor": FLOOR_A}
        rows = _floor_events(world)
        assert json.loads(rows[0]["payload"]) == {"decision": "D-3066",
                                                 "sha_floor": FLOOR_A,
                                                 "epoch": epoch}
    finally:
        _shutdown(world, srv, thread)


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer"])
def test_http_waive_floor_non_router_is_403(tmp_path, who):
    world, srv, thread, key, epoch = _legacy_api(tmp_path)
    try:
        status, body = _call(srv, "/v1/waive_floor",
                             body={"key": key, "decision": "D-3066"},
                             token=world.tok(who))
        assert status == 403, body
        assert body["error"] == "forbidden"
        assert _floor_events(world) == []
    finally:
        _shutdown(world, srv, thread)


@pytest.mark.parametrize("decision", BAD_DECISIONS)
def test_http_waive_floor_bad_decision_is_validation(tmp_path, decision):
    world, srv, thread, key, epoch = _legacy_api(tmp_path)
    try:
        # Extra fields change nothing: the decision is still validated.
        status, body = _call(srv, "/v1/waive_floor",
                             body={"key": key, "decision": decision,
                                   "role": "router", "extra": "x"},
                             token=world.tok("router"))
        assert status == 400, body
        assert body["error"] == "validation"
        assert _floor_events(world) == []
    finally:
        _shutdown(world, srv, thread)


def test_http_waive_floor_missing_fields_are_validation(tmp_path):
    world, srv, thread, key, epoch = _legacy_api(tmp_path)
    try:
        status, body = _call(srv, "/v1/waive_floor", body={"key": key},
                             token=world.tok("router"))
        assert status == 400 and body["error"] == "validation"
        assert _floor_events(world) == []
    finally:
        _shutdown(world, srv, thread)


# ----------------------------------------------------------------------- MCP
def test_mcp_waive_floor_tool_schema_and_route():
    names = {t["name"] for t in mcp.TOOLS}
    assert "waive_floor" in names
    tool = next(t for t in mcp.TOOLS if t["name"] == "waive_floor")
    schema = tool["inputSchema"]
    assert schema["required"] == ["key", "decision"]
    assert set(schema["properties"]) == {"key", "decision", "sha_floor"}
    assert schema["properties"]["sha_floor"] == {"type": "string"}
    assert mcp._ROUTES["waive_floor"] == ("POST", "waive_floor")


def test_mcp_waive_floor_passes_args_through():
    client = _FakeClient()
    reply = _mcp_call(client, "waive_floor",
                      {"key": "demo:A", "decision": "D-3066",
                       "sha_floor": FLOOR_A})
    assert reply["result"]["isError"] is False
    assert client.calls == [("POST", "waive_floor",
                             {"key": "demo:A", "decision": "D-3066",
                              "sha_floor": FLOOR_A})]


# ----------------------------------------------------------------------- CLI
def test_cli_waive_floor_parses():
    from plangraph.keeper import cli
    parser = cli.build_parser()
    args = parser.parse_args(["waive-floor", "demo:A", "--decision", "D-3066"])
    assert args.key == "demo:A"
    assert args.decision == "D-3066"
    assert args.sha_floor is None
    args = parser.parse_args(["waive-floor", "demo:A", "--decision", "D-3066",
                              "--sha-floor", FLOOR_A])
    assert args.sha_floor == FLOOR_A
    with pytest.raises(SystemExit):                      # --decision required
        parser.parse_args(["waive-floor", "demo:A"])


@pytest.mark.parametrize("with_sha", [False, True])
def test_cli_waive_floor_posts_the_right_body(with_sha):
    from plangraph.keeper import cli_verbs

    class _FakeSession:
        json_mode = False

        def __init__(self):
            self.calls = []

        def call(self, method, verb, params=None, body=None):
            self.calls.append((method, verb, body))
            return {"key": body["key"], "decision": body["decision"],
                    "sha_floor": body.get("sha_floor")}

    class _Args:
        key = "demo:A"
        decision = "D-3066"
        sha_floor = FLOOR_A if with_sha else None

    session = _FakeSession()
    assert cli_verbs.cmd_waive_floor(_Args, session) == 0
    assert session.calls == [("POST", "waive_floor",
                              {"key": "demo:A", "decision": "D-3066",
                               "sha_floor": FLOOR_A if with_sha else None})]


# ------------------------------------------------- no other door grows it
def test_apply_ops_still_does_not_expose_waive_floor():
    from plangraph.keeper import store_ops
    assert "waive_floor" not in store_ops._OPS
    assert "waive_floor" not in store_ops._MOVES
