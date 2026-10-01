"""D-217 second-family waiver (part 2 of the two-reviewer gate).

A router may record a verified ``decision`` that waives the "second reviewer
from a different family" requirement for one item; the two passing verdicts may
then share a family.  Everything else about the gate (distinct principals,
non-null families, neither claimant nor delegate) still applies.
"""
import io
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from keeper_util import World, defn, pass_review, seed, to_in_review
from plangraph.keeper import api, auth, cli, config
from plangraph.keeper import events as keeper_events
from plangraph.keeper import mcp
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


KEY = "demo:A"
DECISION = "D-217"


def _count(world, key, event_type):
    return world.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type=?",
        (key, event_type)).fetchone()[0]


def _same_family_reviewer(world, name="reviewerC2"):
    token = auth.add_principal(world.conn, name, "reviewer", ["demo"],
                               family="teamC")
    return auth.authenticate(world.conn, token)


# ----------------------------------------------------------------- store verb

def test_waive_requires_router(w):
    to_in_review(w, KEY)
    with pytest.raises(Refused) as exc:
        w.store.waive_second_family(w.p("reviewer"), KEY, DECISION)
    assert exc.value.code == "forbidden"
    assert w.store._item(KEY)["review_waiver"] is None
    assert _count(w, KEY, "waiver_set") == 0


def test_waive_requires_verified_decision(w):
    to_in_review(w, KEY)
    w.verifiers["decision"].ok = False
    with pytest.raises(Refused) as exc:
        w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(KEY)["review_waiver"] is None
    assert _count(w, KEY, "waiver_set") == 0


def test_waive_refused_on_done_and_open(w):
    to_in_review(w, KEY)
    pass_review(w, KEY)
    assert w.store._item(KEY)["state"] == "done"
    with pytest.raises(Refused) as exc:
        w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert exc.value.code == "locked"
    other = "demo:B"
    w.store.create_items(w.p("router"),
                         [defn("B", []), defn("GOALB", ["B"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.waive_second_family(w.p("router"), other, DECISION)
    assert exc.value.code == "bad_state"


def test_waive_sets_column_and_event(w):
    to_in_review(w, KEY)
    result = w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert result["state"] == "in_review"
    assert result["review_waiver"] == DECISION
    assert w.store._item(KEY)["review_waiver"] == DECISION
    assert _count(w, KEY, "waiver_set") == 1
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


# ------------------------------------------------------------------ the effect

def test_waiver_lets_same_family_second_reviewer_reach_done(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    second = _same_family_reviewer(w)
    first_result = w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    assert first_result["state"] == "in_review"
    assert first_result["reviews_passed"] == 1
    done_result = w.store.verdict(second, KEY, True, "waived family")
    assert done_result["state"] == "done"
    assert done_result["reviews_passed"] == 2
    assert w.store._item(KEY)["state"] == "done"
    assert _count(w, KEY, "review_passed") == 1
    assert w.store.verify_integrity() == []


def test_waiver_still_refuses_same_principal_twice(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), KEY, True, "again")
    assert exc.value.code == "same_principal"
    assert w.store._item(KEY)["state"] == "in_review"


def test_waiver_still_refuses_family_null_reviewer(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewerNoFam"), KEY, True, "who?")
    assert exc.value.code == "family_unknown"
    assert w.store._item(KEY)["state"] == "in_review"


def test_waiver_fold_and_cache_agree(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    folded = keeper_events.fold(keeper_events.read_events(w.conn))
    assert folded[KEY]["review_waiver"] == DECISION
    assert w.store._item(KEY)["review_waiver"] == DECISION
    assert w.store.verify_integrity() == []


# -------------------------------------------------------------- read surfaces

def test_show_exposes_waiver_and_reviews(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    info = w.store.show(w.p("reader"), KEY)
    assert info["review_waiver"] == DECISION
    assert info["reviews"] == [{"reviewer": "reviewer", "family": "teamc"}]
    assert DECISION in __import__("plangraph.keeper.fmt", fromlist=["x"]).show_text(info)


def test_digest_exposes_waivers(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    second = _same_family_reviewer(w)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    w.store.verdict(second, KEY, True, "ok")
    digest = w.store.digest(w.p("router"))
    assert {"key": KEY, "decision_id": DECISION} in digest["waivers"]
    from plangraph.keeper import fmt
    assert DECISION in fmt.digest_text(digest)


def test_snapshot_exposes_waiver_and_reviews(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    snap = w.store.snapshot(w.p("reader"))
    item = {it["key"]: it for it in snap["items"]}[KEY]
    assert item["review_waiver"] == DECISION
    assert item["reviews"] == [{"reviewer": "reviewer", "family": "teamc"}]


# ------------------------------------------------------------------------ API

def _make_cfg(tmp_path, world):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def srv(w, tmp_path):
    server = api.make_server(w.store, _make_cfg(tmp_path, w), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _call(srv, token, body):
    req = urllib.request.Request(srv.base_url + "/v1/waive_second_family",
                                 data=json.dumps(body).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_waive_forbidden_for_non_router(srv, w):
    to_in_review(w, KEY)
    status, body = _call(srv, w.tok("reviewer"), {"key": KEY,
                                                  "decision_id": DECISION})
    assert status == 403
    assert body["error"] == "forbidden"


def test_api_waive_succeeds_for_router(srv, w):
    to_in_review(w, KEY)
    status, body = _call(srv, w.tok("router"), {"key": KEY,
                                                "decision_id": DECISION})
    assert status == 200
    assert body["review_waiver"] == DECISION


# --------------------------------------------------------------- CLI + MCP

@pytest.fixture
def live(w, tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, w)
    server = api.make_server(w.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tf = tmp_path / "router.token"
    tf.write_text(w.tok("router"), encoding="utf-8")
    os.chmod(tf, 0o600)
    monkeypatch.setenv("PG_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("PG_TOKEN_FILE", str(tf))
    yield server, w
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_cli_waive_smoke(live, capsys):
    _, w = live
    to_in_review(w, KEY)
    assert cli.main(["waive", KEY, DECISION]) == 0
    capsys.readouterr()
    assert w.store._item(KEY)["review_waiver"] == DECISION


def test_mcp_waive_tool_smoke():
    names = {tool["name"] for tool in mcp.TOOLS}
    assert "waive_second_family" in names
    assert mcp._ROUTES["waive_second_family"] == ("POST", "waive_second_family")

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, method, verb, args):
            self.calls.append((method, verb, dict(args)))
            return "ok", False

    client = FakeClient()
    src = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "waive_second_family",
                                 "arguments": {"key": KEY,
                                               "decision_id": DECISION}}}) + "\n"
    out = io.StringIO()
    mcp.serve(io.StringIO(src), out, client)
    assert client.calls == [("POST", "waive_second_family",
                             {"key": KEY, "decision_id": DECISION})]
    assert json.loads(out.getvalue())["result"]["isError"] is False
