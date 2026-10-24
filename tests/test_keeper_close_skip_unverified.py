"""Legacy server:OPENCODE-PW-ROTATE evidence must not poison a valid close."""
import hashlib
import os
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "server:OPENCODE-PW-ROTATE"


@pytest.fixture(params=["sqlite", pytest.param(
    "postgres", marks=pytest.mark.skipif(not os.environ.get("PG_DSN"),
                                        reason="PG_DSN not configured"))])
def world(tmp_path, request):
    w = World(tmp_path, backend=request.param)
    w.store.create_items(w.p("router"), [
        defn("OPENCODE-PW-ROTATE", project="server", kind="operator_task",
             review_tier="high", evidence_kinds=["decision"]),
        defn("GOAL", [KEY], project="server", kind="goal"),
    ])
    w.store.set_legacy_state(w.p("router"), KEY, "claimed")
    decisions = tmp_path / "DECISIONS.md"
    decisions.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                         f"| D-2411 | operator close | Closes: {KEY} |\n",
                         encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(decisions))
    batch = tmp_path / "batch.md"
    batch.write_bytes(b"operator close batch\n")
    w.store.batch_roots = [str(tmp_path)]
    w.batch_ref = f"{batch}@{hashlib.sha256(batch.read_bytes()).hexdigest()}"
    # Reproduce the pre-validation stored row, not a newly accepted write.
    item = w.store._item(KEY)
    with w.store._write():
        w.conn.execute(
            "INSERT INTO evidence(key,epoch,kind,ref,verified,detail,at)"
            " VALUES(?,?,?,?,?,?,?)",
            (KEY, item["epoch"], "decision", events.canonical({"d_id": "D-2411"}),
             0, "missing id", w.store._iso(w.clock())))
    yield w
    w.close()


def test_close_skips_unverified_d_id_with_verified_id(world):
    world.store.add_evidence_router(world.p("router"), KEY, "decision",
                                    {"id": "D-2411"})
    out = world.store.close_on_decision(world.p("router"), KEY, world.batch_ref)
    assert out["state"] == "done"
    assert out["decision_ids"] == ["D-2411"]
    assert world.store._item(KEY)["state"] == "done"


def test_close_refuses_unverified_d_id_alone(world):
    with pytest.raises(Refused) as exc:
        world.store.close_on_decision(world.p("router"), KEY, world.batch_ref)
    assert exc.value.code == "evidence_missing"
    assert world.store._item(KEY)["state"] == "claimed"


@pytest.mark.parametrize("router", [True, False])
@pytest.mark.parametrize("ref", ["D-2411", {"d_id": "D-2411"}, {}, {"id": 2411},
                                  {"id": None}, {"id": ""}, {"id": "  "}])
def test_decision_write_requires_string_id(tmp_path, router, ref):
    w = World(tmp_path)
    try:
        w.store.create_items(w.p("router"), [
            defn("A", evidence_kinds=["decision"]),
            defn("G", ["A"], kind="goal"),
        ])
        epoch = to_claimed(w, "demo:A")
        with pytest.raises(Refused) as exc:
            if router:
                w.store.add_evidence_router(w.p("router"), "demo:A", "decision", ref)
            else:
                w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "decision", ref)
        assert exc.value.code == "validation"
        assert exc.value.msg == "decision ref requires a non-empty string id"
        assert not w.store._evidence("demo:A")
    finally:
        w.close()
