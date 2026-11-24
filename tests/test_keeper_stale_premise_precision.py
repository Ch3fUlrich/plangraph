"""KEEPER-STALE-PREMISE-PRECISION: the premise gate ignores the decision that
defines or orders the item, and one verb re-confirms a premise.

The clock starts at 22:13:20Z; decision rows carry the ``(HH:MMZ`` stamp the
decisions log uses, so a row stamped within two hours after the define counts
as logged with it.
"""
from __future__ import annotations

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter


@pytest.fixture
def decisions(tmp_path):
    path = tmp_path / "decisions-log.md"
    path.write_text("| ID | project | decision |\n|---|---|---|\n", encoding="utf-8")
    return path


@pytest.fixture
def w(tmp_path, decisions):
    world = World(tmp_path)
    world.store.ctx = {"decisions": DecisionsAdapter(decisions)}
    try:
        yield world
    finally:
        world.close()


def _decide(path, did, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"| D-{did} | demo | {text} |\n")


def _items(w, **extra):
    w.store.create_items(w.p("router"), [
        defn("PROD-FROM-MAIN-ONLY", description="per D-5800", **extra.get("PROD", {})),
        defn("SPAWNER-DEFAULT", description="per D-5800"),
        defn("PHASE-LINE-GREEN", description="per D-5800"),
        defn("LESSON-ITEM", description="per D-5800"),
        defn("SCOPE-ITEM", description="per D-5800"),
        defn("GOAL", ["PROD-FROM-MAIN-ONLY", "SPAWNER-DEFAULT", "PHASE-LINE-GREEN",
                      "LESSON-ITEM", "SCOPE-ITEM"], kind="goal")])


def _claims(w, key):
    try:
        return w.store.claim(w.p("l1main"), key)["epoch"] == 1
    except Refused as exc:
        assert "premise outdated" in exc.msg, exc.msg
        return False


def test_the_four_cases_of_2026_11_13_claim_without_force(w, decisions):
    _items(w, PROD={"source": "D-5862"})
    # (a) the item's own source decision
    _decide(decisions, 5862, "(05:0xZ) PROD-FROM-MAIN-ONLY discussion of the rollout")
    # (b) a row that orders or GOes the item without changing scope
    _decide(decisions, 5858, "(05:0xZ) OPERATOR order: demo:SPAWNER-DEFAULT ordered first, GO")
    # (c) logged within two hours after the define
    _decide(decisions, 5837, "(23:4xZ) note on demo:PHASE-LINE-GREEN wording")
    _decide(decisions, 5853, "(00:1xZ) lesson items: demo:LESSON-ITEM queued in the plan")
    assert _claims(w, "demo:PROD-FROM-MAIN-ONLY")
    assert _claims(w, "demo:SPAWNER-DEFAULT")
    assert _claims(w, "demo:PHASE-LINE-GREEN")
    assert _claims(w, "demo:LESSON-ITEM")


def test_a_decision_that_changes_scope_after_the_define_still_refuses(w, decisions):
    _items(w)
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM now also covers the web; supersedes the old scope")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")
    assert "premise outdated: D-5900" in exc.value.msg
    # a late, unmarked row names the item too: still refused (not within 2 h)
    _decide(decisions, 5901, "(09:3xZ) demo:SPAWNER-DEFAULT premise changed")
    assert not _claims(w, "demo:SPAWNER-DEFAULT")


def test_reconfirm_clears_the_premise_without_an_edit_and_is_audited(w, decisions):
    _items(w)
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM premise changed, start after P7")
    with pytest.raises(Refused):
        w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")
    before = w.store.show(w.p("router"), "demo:SCOPE-ITEM")["description"]
    result = w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900")
    assert result == {"key": "demo:SCOPE-ITEM", "reconfirmed": "D-5900"}
    assert w.store.show(w.p("router"), "demo:SCOPE-ITEM")["description"] == before
    assert w.store.show(w.p("router"), "demo:SCOPE-ITEM")["newer_decisions"] == []
    from plangraph.keeper import events as keeper_events
    marks = [e for e in keeper_events.read_events(w.conn)
             if e["key"] == "demo:SCOPE-ITEM" and e["payload"].get("reconfirmed") == "D-5900"]
    assert marks and marks[0]["actor"] == "router"
    # a newer decision after the reconfirm refuses again
    _decide(decisions, 5950, "(07:0xZ) demo:SCOPE-ITEM changes again")
    assert w.store.show(w.p("router"), "demo:SCOPE-ITEM")["newer_decisions"] == ["D-5950"]
    assert w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM", force_stale="x")["epoch"] == 1


def test_reconfirm_also_clears_a_retired_premise(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-9")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_reconfirm_refuses_an_unknown_decision_and_a_stranger(w, decisions):
    _items(w)
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-77777")
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM premise changed")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("l1other"), "demo:SCOPE-ITEM", "D-5900")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "not-a-decision")


def test_alerts_report_the_force_stale_claims(w, decisions):
    _items(w)
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM premise changed")
    assert w.store.alerts(w.p("router"))["force_stale_claims"] == 0
    w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM", force_stale="read D-5900")
    assert w.store.alerts(w.p("router"))["force_stale_claims"] == 1
