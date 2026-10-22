"""X7c: the router ``set_legacy_state`` verb and its API/CLI/MCP doors.

``set_legacy_state`` opens an ``open`` legacy row into ``claimed``/``in_review``
so that ``close_on_decision`` (X7b) can then close it; every check runs under
one ``BEGIN IMMEDIATE`` and the ``legacy_state_set`` fold keeps the cache and
the hash chain agreeing.  The doors below mirror the waiver route: the verb
refuses a non-router, and the API maps ``forbidden`` to 403.

KEEPER-OPCLAIM-CLOSE: ``claimed`` is refused on any tier other than ``none``
(a legacy claim nobody can submit is a dead end), and a row the pre-fix verb
already stranded in ``claimed`` (no lease, owner claimant, ``legacy_state_set``
last) may still move to ``in_review`` to rejoin the normal review rule.
"""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import api, auth, config
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
DID = "D-400"


# --------------------------------------------------------------------- verb
def _legacy_world(tmp_path, *, review_tier=None, kind=None, owner_family=None):
    """A lane the verb acts on plus the mandatory goal.

    ``seed`` cannot be used when the lane needs a tier/kind of its own: it
    passes the same definition kwargs to the goal, and a tier-``none`` goal is
    refused at create.
    """
    world = World(tmp_path)
    lane = defn("A", [])
    if review_tier is not None:
        lane["review_tier"] = review_tier
        if review_tier == "none":
            lane["evidence_kinds"] = ["decision"]
    if kind is not None:
        lane["kind"] = kind
    if owner_family is not None:
        lane["owner_family"] = owner_family
    world.store.create_items(world.p("router"),
                             [lane, defn("GOAL", ["A"], kind="goal")])
    return world


def _seed_none(world):
    """The tier-``none`` decision lane ``_close_world`` seeds (API/CLI doors)."""
    world.store.create_items(world.p("router"), [
        defn("A", [], review_tier="none", evidence_kinds=["decision"]),
        defn("GOAL", ["A"], kind="goal")])
    return world


def _stranded(world, key=KEY):
    """Write the row the *pre-fix* verb used to produce (KEEPER-OPCLAIM-CLOSE).

    ``set_legacy_state(claimed)`` now refuses a tiered lane, so the stranded
    shape is rebuilt here event-for-row: one ``legacy_state_set`` carrying the
    owner, epoch and round, plus the matching cache UPDATE, all inside the
    store's own write transaction so ``fold`` agrees from the first check.
    """
    item = world.store._item(key)
    owner = item["owner"]
    epoch = int(item["epoch"]) + 1
    round_no = int(item.get("round") or 0) + 1
    now = world.clock()
    with world.store._write():
        world.store._append(world.p("router"), key, "legacy_state_set",
                            {"state": "claimed", "note": "backfill",
                             "legacy": True, "claimed_by": owner,
                             "epoch": epoch, "round": round_no}, now)
        cursor = world.conn.execute(
            "UPDATE items SET state='claimed', epoch=?, round=?, claimed_by=?,"
            " delegate=NULL, reviewer=NULL, lease_until=NULL,"
            " review_waiver=NULL, updated_at=? WHERE key=? AND state='open'",
            (epoch, round_no, owner, world.store._iso(now), key))
        assert cursor.rowcount == 1, "stranded build: item was not open"
    assert world.store.verify_integrity() == [], "stranded build must fold clean"
    return world


@pytest.fixture
def w(tmp_path):
    """The default standard-tier lane: the review path a legacy claim would strand."""
    world = _legacy_world(tmp_path)
    yield world
    world.close()


@pytest.fixture
def w_none(tmp_path):
    """A tier-``none`` decision lane: the row the verb was written for."""
    world = _legacy_world(tmp_path, review_tier="none")
    yield world
    world.close()


def test_open_to_claimed(w_none):
    result = w_none.store.set_legacy_state(w_none.p("router"), KEY, "claimed",
                                           "backfill")
    assert result["state"] == "claimed"
    assert result["claimed_by"] == "tester"
    assert w_none.store._item(KEY)["state"] == "claimed"
    assert w_none.store._item(KEY)["claimed_by"] == "tester"
    assert w_none.store.verify_integrity() == []


def test_claimed_on_standard_tier_is_bad_state(w):
    """The stranding state is unreachable: a standard lane may not go claimed."""
    before = dict(w.store._item(KEY))
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), KEY, "claimed", "backfill")
    assert exc.value.code == "bad_state"
    assert "standard" in exc.value.msg      # the message names the tier
    assert "in_review" in exc.value.msg      # ...and the way that is allowed
    after = dict(w.store._item(KEY))
    assert after["state"] == "open"
    for field in ("epoch", "round", "claimed_by", "delegate", "reviewer",
                  "lease_until", "review_waiver"):
        assert after[field] == before[field]
    assert [e["type"] for e in keeper_events.read_events(w.conn)].count(
        "legacy_state_set") == 0            # no event, no partial write
    assert w.store.verify_integrity() == []


def test_claimed_on_tier_none_still_moves(w_none):
    out = w_none.store.set_legacy_state(w_none.p("router"), KEY, "claimed")
    assert out["state"] == "claimed" and out["claimed_by"] == "tester"
    assert w_none.store._item(KEY)["lease_until"] is None
    assert w_none.store.verify_integrity() == []


def test_claimed_on_operator_task_still_moves(tmp_path):
    world = _legacy_world(tmp_path, kind="operator_task")
    item = world.store._item(KEY)
    assert item["kind"] == "operator_task" and item["review_tier"] == "standard"
    out = world.store.set_legacy_state(world.p("router"), KEY, "claimed")
    assert out["state"] == "claimed" and out["claimed_by"] == "tester"
    assert world.store.verify_integrity() == []
    world.close()


def test_stranded_legacy_claim_recovers_to_in_review_and_reviews_to_done(tmp_path):
    """The live case: recover it, then close it through the normal tier rule."""
    world = _legacy_world(tmp_path, owner_family="teamZ")
    _stranded(world)
    stuck = world.store._item(KEY)
    assert (stuck["state"], stuck["lease_until"], stuck["claimed_by"]) == (
        "claimed", None, "tester")
    out = world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert out["state"] == "in_review" and out["claimed_by"] == "tester"
    item = world.store._item(KEY)
    assert item["state"] == "in_review"
    assert item["epoch"] == stuck["epoch"] + 1          # epoch + 1
    assert item["round"] == stuck["round"] + 1          # fresh round
    assert item["delegate"] is None and item["reviewer"] is None
    assert item["lease_until"] is None and item["review_waiver"] is None
    # Verified evidence plus one verdict from a reviewer of another family:
    # the standard tier's normal rule, no special-casing of legacy rows.
    world.store.add_evidence_router(world.p("router"), KEY, "decision",
                                    {"id": DID})
    done = world.store.verdict(world.p("reviewer"), KEY, True, "passed")
    assert done["state"] == "done" and done["reviews_passed"] == 1
    assert world.store._item(KEY)["state"] == "done"
    assert world.store.verify_integrity() == []
    world.close()


def test_real_claim_keeps_bad_state(w):
    """A real claim (lease set) is not a legacy claim: in_review stays refused."""
    w.store.claim(w.p("l1main"), KEY)
    claimed = w.store._item(KEY)
    assert claimed["lease_until"] is not None
    assert claimed["claimed_by"] == "l1main"
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    assert exc.value.code == "bad_state"
    after = w.store._item(KEY)
    assert (after["state"], after["epoch"], after["lease_until"]) == (
        claimed["state"], claimed["epoch"], claimed["lease_until"])
    assert w.store.verify_integrity() == []


def test_reclaimed_after_a_legacy_claim_keeps_bad_state(tmp_path):
    """A later claim event (release -> claim) also ends the recovery window."""
    world = _legacy_world(tmp_path)
    _stranded(world)
    epoch = world.store._item(KEY)["epoch"]
    world.store.release(world.p("router"), KEY, epoch)
    world.store.claim(world.p("l1main"), KEY)
    with pytest.raises(Refused) as exc:
        world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert exc.value.code == "bad_state"
    assert world.store._item(KEY)["state"] == "claimed"
    assert world.store.verify_integrity() == []
    world.close()


def test_recovery_keeps_fold_and_integrity_clean(tmp_path):
    world = _legacy_world(tmp_path, owner_family="teamZ")
    _stranded(world)
    assert world.store.verify_integrity() == []
    world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    item = world.store._item(KEY)
    folded = keeper_events.fold(keeper_events.read_events(world.conn))[KEY]
    assert folded["state"] == item["state"] == "in_review"
    assert folded["epoch"] == item["epoch"] == 2
    assert folded["round"] == item["round"]
    assert folded["claimed_by"] == item["claimed_by"] == "tester"
    assert folded["lease_until"] is None
    assert world.store.verify_integrity() == []
    assert world.store.verify_chain() == []
    world.close()


def test_open_to_in_review(w):
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    assert w.store._item(KEY)["state"] == "in_review"
    assert w.store.verify_integrity() == []


def test_records_legacy_state_set_event(w_none):
    w_none.store.set_legacy_state(w_none.p("router"), KEY, "claimed", "note")
    types = [e["type"] for e in __import__(
        "plangraph.keeper.events", fromlist=["x"]).read_events(w_none.conn)]
    assert "legacy_state_set" in types
    assert w_none.store.verify_chain() == []


def test_non_router_forbidden(w):
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("l1main"), KEY, "claimed")
    assert exc.value.code == "forbidden"
    assert w.store._item(KEY)["state"] == "open"


@pytest.mark.parametrize("target", ["done", "open", "cancelled", "bogus"])
def test_bad_target_is_validation(w, target):
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), KEY, target)
    assert exc.value.code == "validation"


def test_non_open_item_is_bad_state(w):
    # ``in_review`` first (allowed from open), then any further move: bad_state.
    # A *stranded* ``claimed`` row is the one non-open state ``in_review``
    # still accepts -- see ``test_stranded_legacy_claim_recovers_...``.
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), KEY, "claimed")
    assert exc.value.code == "bad_state"


def test_cancelled_item_is_bad_state(w):
    w.store.cancel(w.p("router"), KEY, "drop")
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), KEY, "claimed")
    assert exc.value.code == "bad_state"


def test_missing_key_not_found(w):
    with pytest.raises(Refused) as exc:
        w.store.set_legacy_state(w.p("router"), "demo:Z", "claimed")
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------- API
def _api_server(tmp_path, world, extra_cfg=None):
    data = {"db": str(world.path),
            "snapshot": str(tmp_path / "plan.snapshot.json"),
            "bind": "127.0.0.1:0"}
    data.update(extra_cfg or {})
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    cfg = config.load_config(path)
    server = api.make_server(world.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread


def _call(srv, path, *, method="POST", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.base_url + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_api_set_legacy_state_happy(tmp_path):
    world = World(tmp_path)
    _seed_none(world)
    srv, thread = _api_server(tmp_path, world)
    try:
        status, body = _call(srv, "/v1/set_legacy_state",
                             body={"key": KEY, "state": "claimed",
                                   "note": "via api"}, token=world.tok("router"))
        assert status == 200, body
        assert body["state"] == "claimed"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        world.close()


def test_api_set_legacy_state_non_router_403(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    srv, thread = _api_server(tmp_path, world)
    try:
        status, body = _call(srv, "/v1/set_legacy_state",
                             body={"key": KEY, "state": "claimed"},
                             token=world.tok("l1main"))
        assert status == 403
        assert body["error"] == "forbidden"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        world.close()


def _close_world(tmp_path, *, roots=None):
    """A tier-none lane moved into ``claimed`` by the new verb, with a verified
    ``decision`` attached, ready for ``close_on_decision`` through the API."""
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A", [], review_tier="none", evidence_kinds=["decision"]),
        defn("GOAL", ["A"], kind="goal")])
    dec = tmp_path / "DECISIONS.md"
    dec.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                   f"| {DID} | recorded | no token |\n", encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(dec))
    world.store.set_legacy_state(world.p("router"), KEY, "claimed")
    world.store.add_evidence_router(world.p("router"), KEY, "decision",
                                    {"id": DID})
    return world


def _batch_file(tmp_path, text="batch body\n"):
    root = tmp_path / "plan"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "batch.md"
    path.write_text(text, encoding="utf-8")
    return path, f"{path}@{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_api_close_refused_without_batch_roots(tmp_path):
    world = _close_world(tmp_path)
    srv, thread = _api_server(tmp_path, world)   # config declares no batch_roots
    try:
        status, body = _call(srv, "/v1/close_on_decision",
                             body={"key": KEY, "batch_ref": "x@" + "0" * 64},
                             token=world.tok("router"))
        assert status == 403, body
        assert body["error"] == "forbidden"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        world.close()


def test_api_close_happy_path_e2e(tmp_path):
    """config.batch_roots -> make_server -> store -> verb: the full door."""
    path, ref = _batch_file(tmp_path)
    world = _close_world(tmp_path)
    srv, thread = _api_server(tmp_path, world,
                              extra_cfg={"batch_roots": [str(tmp_path / "plan")]})
    try:
        # wiring put the configured root on the store the server built with.
        assert world.store.batch_roots == [str(path.parent)]
        status, body = _call(srv, "/v1/close_on_decision",
                             body={"key": KEY, "batch_ref": ref},
                             token=world.tok("router"))
        assert status == 200, body
        assert body["state"] == "done"
        assert world.store._item(KEY)["state"] == "done"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        world.close()


# --------------------------------------------------------------------- CLI
def test_cli_close_and_legacy_parse():
    from plangraph.keeper import cli
    parser = cli.build_parser()
    close = parser.parse_args(["close", KEY, "--batch", "p@" + "a" * 64])
    assert close.key == KEY and close.batch == "p@" + "a" * 64
    legacy = parser.parse_args(["legacy", KEY, "claimed", "a note"])
    assert legacy.state == "claimed" and legacy.note == "a note"
    define = parser.parse_args(["define", "--file", "f.json", "--tier", "none"])
    assert define.tier == "none"


@pytest.fixture
def cli_live(tmp_path, monkeypatch):
    world = World(tmp_path)
    _seed_none(world)
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({"db": str(world.path),
                                    "snapshot": str(tmp_path / "plan.snapshot.json"),
                                    "bind": "127.0.0.1:0"}), encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    cfg = config.load_config(cfg_path)
    server = api.make_server(world.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PG_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("PG_CONFIG", str(cfg_path))
    toks = {}
    for name in ("router", "l1main"):
        tf = tmp_path / f"{name}.token"
        tf.write_text(world.tok(name), encoding="utf-8")
        os.chmod(tf, 0o600)
        toks[name] = tf
    yield world, toks
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    world.close()


def _cli_run(token_file, *argv):
    os.environ["PG_TOKEN_FILE"] = str(token_file)
    from plangraph.keeper import cli
    return cli.main(list(argv))


def test_cli_legacy_happy_and_refusal(cli_live):
    world, toks = cli_live
    assert _cli_run(toks["router"], "legacy", KEY, "claimed", "backfill") == 0
    assert world.store._item(KEY)["state"] == "claimed"
    # A non-router hits the same forbidden wall the API returns as 403 -> exit 2.
    assert _cli_run(toks["l1main"], "legacy", "demo:GOAL", "claimed") == 2
    assert world.store._item("demo:GOAL")["state"] == "open"


# --------------------------------------------------------------------- MCP
class _FakeClient:
    def __init__(self, text="ok", is_error=False):
        self.text, self.is_error = text, is_error
        self.calls = []

    def call(self, method, verb, args):
        self.calls.append((method, verb, args))
        return self.text, self.is_error


def _mcp_call(client, name, arguments):
    from plangraph.keeper import mcp
    return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}, client)


def test_mcp_lists_new_tools():
    from plangraph.keeper import mcp
    names = [t["name"] for t in mcp.TOOLS]
    assert "close_on_decision" in names and "set_legacy_state" in names
    assert "close_on_decision" in mcp._ROUTES
    assert "set_legacy_state" in mcp._ROUTES


def test_mcp_set_legacy_routes_and_passes_args():
    client = _FakeClient()
    reply = _mcp_call(client, "set_legacy_state",
                      {"key": KEY, "state": "claimed", "note": "n"})
    assert reply["result"]["isError"] is False
    assert client.calls == [("POST", "set_legacy_state",
                             {"key": KEY, "state": "claimed", "note": "n"})]


def test_mcp_close_surfaces_refusal():
    client = _FakeClient("forbidden: no batch_roots", is_error=True)
    reply = _mcp_call(client, "close_on_decision",
                      {"key": KEY, "batch_ref": "p@" + "0" * 64})
    assert reply["result"]["isError"] is True
    assert "forbidden" in reply["result"]["content"][0]["text"]

