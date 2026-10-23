"""X7c: the router ``set_legacy_state`` verb and its API/CLI/MCP doors.

``set_legacy_state`` opens an ``open`` legacy row into ``claimed``/``in_review``
so that ``close_on_decision`` (X7b) can then close it; every check runs under
one ``BEGIN IMMEDIATE`` and the ``legacy_state_set`` fold keeps the cache and
the hash chain agreeing.  The doors below mirror the waiver route: the verb
refuses a non-router, and the API maps ``forbidden`` to 403.

KEEPER-OPCLAIM-CLOSE: ``claimed`` is refused on any tier other than ``none``
for a *reviewed* kind (a legacy claim nobody can submit is a dead end), while
the ``operator_task`` exemption stands: that kind is never agent-reviewed, so
``close_on_decision`` (KEEPER-OPTASK-CLOSE, #129) closes it from ``claimed`` at
any tier on a decision whose whole ``Closes: <key>`` token names the item —
the terminal-state test below walks that path to ``done`` at ``standard`` and
``high``.  A row the pre-fix verb already stranded in ``claimed`` (no lease,
owner claimant, holder columns NULL, ``legacy_state_set`` last) may still move
to ``in_review`` to rejoin the normal review rule, but only on a row where
``claimed`` itself is refused: a tier-``none`` claim and an ``operator_task``
claim are closable, not stranded.
"""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, seed, seed_stranded_claim
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
    """The row the *pre-fix* verb used to produce (KEEPER-OPCLAIM-CLOSE).

    The single seeder lives in ``keeper_util.seed_stranded_claim`` and is
    shared with ``tests/test_keeper_rehome.py``; this file passes its note.
    """
    seed_stranded_claim(world, key, note="backfill")
    return world


def _decision_close(tmp_path, world):
    """The proof ``close_on_decision`` needs: a decisions file whose row names
    ``KEY`` in a whole ``Closes: <key>`` token, plus a hashed batch file inside
    a configured ``batch_root`` (KEEPER-OPTASK-CLOSE).  Returns the ``batch_ref``.
    """
    decisions = tmp_path / "DECISIONS.md"
    decisions.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                         f"| {DID} | closes {KEY} | Closes: {KEY} |\n",
                         encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(decisions))
    root = tmp_path / "plan"
    root.mkdir(parents=True, exist_ok=True)
    batch = root / "batch.md"
    batch.write_text("batch body\n", encoding="utf-8")
    world.store.batch_roots = [str(root)]
    return f"{batch}@{hashlib.sha256(batch.read_bytes()).hexdigest()}"


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
    """The exemption stands (KEEPER-OPCLAIM-CLOSE): ``operator_task`` is not
    a reviewed kind, so the tier that refuses a lane still takes it — and the
    terminal-state test below shows that tier leads to ``done``."""
    world = _legacy_world(tmp_path, kind="operator_task")
    item = world.store._item(KEY)
    assert item["kind"] == "operator_task" and item["review_tier"] == "standard"
    out = world.store.set_legacy_state(world.p("router"), KEY, "claimed")
    assert out["state"] == "claimed" and out["claimed_by"] == "tester"
    assert world.store.verify_integrity() == []
    world.close()


@pytest.mark.parametrize("tier", ["standard", "high"])
def test_operator_task_claimed_reaches_done_on_decision(tmp_path, tier):
    """The item-done path: an operator-claimed standard/high item reaches
    ``done`` with a decision and the seat ref, so the state the verb opens for
    the exempted kind is never a stranding state (KEEPER-OPCLAIM-CLOSE item 2).

    Router ``set_legacy_state(claimed)`` -> verified ``decision`` evidence
    naming the key -> router ``close_on_decision`` (KEEPER-OPTASK-CLOSE, #129)
    writes the cache row ``done`` with ``reviewer`` = the router before the
    ``closed_on_decision`` event locks it.
    """
    world = _legacy_world(tmp_path, kind="operator_task", review_tier=tier)
    batch_ref = _decision_close(tmp_path, world)
    out = world.store.set_legacy_state(world.p("router"), KEY, "claimed")
    assert out["state"] == "claimed" and out["claimed_by"] == "tester"
    assert world.store._item(KEY)["review_tier"] == tier
    world.store.add_evidence_router(world.p("router"), KEY, "decision",
                                    {"id": DID})
    done = world.store.close_on_decision(world.p("router"), KEY, batch_ref)
    assert done["state"] == "done" and done["decision_ids"] == [DID]
    item = world.store._item(KEY)
    assert item["state"] == "done"
    assert item["reviewer"] == "router"        # the seat the close fills
    assert item["claimed_by"] == "tester"
    assert world.store.verify_integrity() == []
    world.close()


def test_stranded_operator_task_is_closable_not_recovered(tmp_path):
    """An ``operator_task`` row in the stranded *shape* is still not stranded:
    recovery stays off it (``claimed`` is allowed for that kind, so the refusal
    scope and the recovery scope are one predicate) and ``close_on_decision``
    takes it to ``done`` instead of leaving it stuck."""
    world = _legacy_world(tmp_path, kind="operator_task", review_tier="standard")
    batch_ref = _decision_close(tmp_path, world)
    _stranded(world)
    assert world.store._item(KEY)["state"] == "claimed"
    with pytest.raises(Refused) as exc:
        world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert exc.value.code == "bad_state"
    assert "operator_task" in exc.value.msg     # why this row is not stranded
    assert world.store._item(KEY)["state"] == "claimed"
    world.store.add_evidence_router(world.p("router"), KEY, "decision",
                                    {"id": DID})
    done = world.store.close_on_decision(world.p("router"), KEY, batch_ref)
    assert done["state"] == "done"
    assert world.store._item(KEY)["state"] == "done"
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
    item = world.store._item(KEY)
    assert item["state"] == "done"
    assert item["reviewer"] == "reviewer"     # the seat the store records as filled
    assert world.store.verify_integrity() == []
    world.close()


def test_stranded_high_tier_claim_recovers_to_in_review(tmp_path):
    """``high`` is the other tier where ``claimed`` is refused, so its
    stranded rows are recoverable too (recovery == the refusal scope)."""
    world = _legacy_world(tmp_path, review_tier="high", owner_family="teamZ")
    _stranded(world)
    out = world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert out["state"] == "in_review"
    item = world.store._item(KEY)
    assert item["review_tier"] == "high"
    assert item["delegate"] is None and item["reviewer"] is None
    assert world.store.verify_integrity() == []
    world.close()


def test_tier_none_stranded_claim_is_not_recovered(tmp_path):
    """A tier-``none`` claim is closable, not stranded: ``close_on_decision``
    fires from ``claimed``, so the recovery deliberately stays refused."""
    world = _legacy_world(tmp_path, review_tier="none")
    _stranded(world)
    before = dict(world.store._item(KEY))
    with pytest.raises(Refused) as exc:
        world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert exc.value.code == "bad_state"
    assert "none" in exc.value.msg
    after = dict(world.store._item(KEY))
    assert after["state"] == "claimed"
    for field in ("epoch", "round", "claimed_by", "delegate", "reviewer",
                  "lease_until", "review_waiver"):
        assert after[field] == before[field]
    assert [e["type"] for e in keeper_events.read_events(world.conn)].count(
        "legacy_state_set") == 1            # the seed's event, no second write
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


def test_router_dep_edit_does_not_defeat_recovery(tmp_path):
    """A router definition edit after the legacy claim rewrites the definition;
    it does not take over the claim (KEEPER-OPCLAIM-CLOSE item 3).

    ``add_dep`` delegates to the router's active-item edit, which appends
    ``item_defined`` and bumps the round *and* the epoch.  ``item_defined``/
    ``kind_set`` are not lifecycle events, so ``legacy_state_set`` is still the
    newest one that counts and the stranded row recovers to ``in_review``.
    """
    world = _legacy_world(tmp_path, owner_family="teamZ")
    world.store.create_items(world.p("router"),
                             [defn("Z", []), defn("G2", ["Z"], kind="goal")])
    _stranded(world)
    stuck = world.store._item(KEY)
    world.store.apply_ops(world.p("router"),
                          [{"op": "add_dep", "key": KEY, "dep": "demo:Z"}])
    edited = world.store._item(KEY)
    assert "demo:Z" in edited["depends_on"] and edited["state"] == "claimed"
    assert (edited["epoch"], edited["round"]) != (stuck["epoch"], stuck["round"])
    types = [e["type"] for e in keeper_events.read_events(world.conn)]
    assert types[-1] == "item_defined"        # the router edit really happened
    assert world.store._last_state_changing(KEY) == "legacy_state_set"
    out = world.store.set_legacy_state(world.p("router"), KEY, "in_review")
    assert out["state"] == "in_review" and out["claimed_by"] == "tester"
    item = world.store._item(KEY)
    assert item["delegate"] is None and item["reviewer"] is None
    assert "demo:Z" in item["depends_on"]
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

