"""B1: the doors of ``apply_ops`` -- HTTP route, MCP tool, apply_batch default."""
import io
import json
import urllib.error
import urllib.request

import pytest

from test_apply_batch import (  # noqa: F401  (fixtures + helpers)
    apply_batch, item_count, ops_for, run, srv, state_of, token_file, world,
    write_batch)
from plangraph.keeper.mcp import HttpKeeper, serve


def _post(srv, token, body):
    request = urllib.request.Request(
        f"{srv.base_url}/v1/apply_ops", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _spy(monkeypatch):
    calls = []
    orig = apply_batch.Session.post

    def spy(self, verb, body):
        calls.append(verb)
        return orig(self, verb, body)
    monkeypatch.setattr(apply_batch.Session, "post", spy)
    return calls


# --------------------------------------------------------------- HTTP route
def test_route_router_applies_and_non_router_is_403(world, srv):
    ops = ops_for()[:2]
    status, body = _post(srv, world.tokens["l1main"], {"ops": ops})
    assert status == 403 and item_count(world) == 0
    status, body = _post(srv, world.tokens["router"], {"ops": ops})
    assert status == 200 and body["applied"] == 2 and item_count(world) == 2


def test_route_refusal_is_rolled_back_with_op_named(world, srv):
    ops = ops_for()[:3]
    ops[1] = {"op": "edit", "key": "demo:MISSING", "fields": {"title": "x"}}
    status, body = _post(srv, world.tokens["router"], {"ops": ops})
    assert status == 404
    assert "op 2 edit demo:MISSING: not_found" in body["msg"]
    assert item_count(world) == 0


def test_route_requires_ops_list(world, srv):
    status, _ = _post(srv, world.tokens["router"], {})
    assert status == 400


# --------------------------------------------------------------- MCP tool
def test_mcp_apply_ops_tool_listed_and_calls_route(world, srv, token_file):
    client = HttpKeeper(srv.base_url, token_file, timeout=10)
    out = io.StringIO()
    serve(io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "apply_ops", "arguments": {
                          "ops": ops_for()[:2]}}}) + "\n"), out, client)
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    assert "apply_ops" in [t["name"] for t in first["result"]["tools"]]
    assert second["result"]["isError"] is False
    assert item_count(world) == 2


# ------------------------------------------------ apply_batch default path
def test_default_mode_is_one_apply_ops_call(world, srv, token_file, tmp_path,
                                            monkeypatch):
    calls = _spy(monkeypatch)
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file)
    assert rc == 0, out + err
    assert calls == ["apply_ops"]
    for n in range(1, 7):
        assert f"ok {n} " in out
    assert "applied 6 of 6" in out
    assert state_of(world, "demo:B") == "done"
    assert world.store.verify_integrity() == []


def test_per_op_flag_keeps_one_call_per_op(world, srv, token_file, tmp_path,
                                           monkeypatch):
    calls = _spy(monkeypatch)
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file,
                       extra=["--per-op"])
    assert rc == 0, out + err
    assert len(calls) == 6 and "apply_ops" not in calls


def test_default_mode_refusal_rolls_back_whole_batch(world, srv, token_file,
                                                     tmp_path):
    ops = ops_for()
    ops[4] = {"op": "cancel", "key": "demo:NOPE"}
    rc, out, err = run(write_batch(tmp_path, ops=ops), srv.base_url, token_file)
    assert rc == 1
    assert "op 5 cancel demo:NOPE: not_found" in out
    assert "nothing applied" in out and "resume with" not in out
    assert item_count(world) == 0 and "ok 1" not in out


def test_default_mode_from_renumbers_the_failing_op(world, srv, token_file,
                                                    tmp_path):
    # B (demo:B) does not exist, so op 3 (legacy_state) fails; with --from 3 the
    # keeper sees it as its op 1 and apply_batch reports it as the file's op 3.
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file,
                       extra=["--from", "3"])
    assert rc == 1
    assert "op 3 legacy_state demo:B: not_found" in out
    assert item_count(world) == 0


def test_token_never_printed_in_default_mode(world, srv, token_file, tmp_path):
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file)
    assert world.tokens["router"] not in out + err
