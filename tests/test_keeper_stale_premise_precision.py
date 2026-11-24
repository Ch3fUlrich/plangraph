"""STALE-PREMISE-WATERMARK-RECONFIRM: the premise gate reads decision ids from
premise fields only, the item's own source decision is never "newer", and
``reconfirm KEY D-NNN`` is the one audited verb that raises a per-item floor
read by every claim path.
"""
from __future__ import annotations

import pytest

from keeper_util import World, defn
from plangraph.keeper import events as keeper_events
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


def _newer(w, key):
    return w.store.show(w.p("router"), key)["newer_decisions"]


def _refuses(w, key):
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key)
    assert "premise outdated" in exc.value.msg, exc.value.msg
    return exc.value.msg


def test_a_session_id_on_one_item_does_not_move_the_watermark_of_another(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("B", description="per D-5800"),
        defn("A", description="per D-5800", session="D-999999"),
        defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 5900, "demo:B now also covers the web; supersedes the old scope")
    assert "D-5900" in _refuses(w, "demo:B")
    assert _newer(w, "demo:B") == ["D-5900"]


def test_metadata_fields_never_carry_an_id_into_the_clock(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("B", description="per D-5800"),
        defn("A", description="per D-5800", similar=["D-999999"], category="D-999999",
             supersedes="D-999999"),
        defn("GOAL", ["A", "B"], kind="goal")])
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B scope changed")
    assert _newer(w, "demo:B") == ["D-5900"]


def test_an_edited_premise_moves_the_watermark_through_its_fields(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    assert _newer(w, "demo:A") == ["D-5900"]
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "per D-5900"})
    assert _newer(w, "demo:A") == []


def test_the_items_own_source_decision_claims_without_force(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", source="D-5862"),
                                         defn("B", description="per D-5800", source="D-5850"),
                                         defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 5862, "demo:A discussion of the rollout")
    assert _newer(w, "demo:A") == []
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    # another row naming the item, newer than the source, still refuses
    _decide(decisions, 5870, "demo:B scope changed")
    assert "D-5870" in _refuses(w, "demo:B")


def test_a_row_that_is_not_the_source_and_not_reconfirmed_keeps_the_gate(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5858, "OPERATOR order: demo:A ordered first, GO")
    assert "D-5858" in _refuses(w, "demo:A")


def test_reconfirm_clears_exactly_up_to_the_decision_and_is_audited(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    _decide(decisions, 5910, "demo:A scope changed again")
    before = w.store.show(w.p("router"), "demo:A")["description"]
    assert w.store.reconfirm(w.p("router"), "demo:A", "D-5900") == {
        "key": "demo:A", "reconfirmed": "D-5900"}
    assert w.store.show(w.p("router"), "demo:A")["description"] == before
    assert _newer(w, "demo:A") == ["D-5910"]               # exactly up to D-5900
    marks = [e for e in keeper_events.read_events(w.conn)
             if e["key"] == "demo:A" and e["type"] == "premise_reconfirmed"]
    assert len(marks) == 1 and marks[0]["actor"] == "router"
    assert marks[0]["payload"]["decision"] == "D-5900"
    w.store.reconfirm(w.p("router"), "demo:A", "D-5910")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    w.store.release(w.p("l1main"), "demo:A", 1)
    _decide(decisions, 5950, "demo:A changes once more")   # a later decision refuses again
    assert "D-5950" in _refuses(w, "demo:A")
    assert _newer(w, "demo:A") == ["D-5950"]


def test_the_floor_is_per_item(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("B", description="per D-5800"),
                                         defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 5900, "demo:A and demo:B scope changed")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert _newer(w, "demo:A") == [] and _newer(w, "demo:B") == ["D-5900"]


def test_reconfirm_also_clears_a_retired_premise(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-9")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_a_system_lease_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("PREM", description="per D-5"),
                                         defn("GOAL", ["PREM"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:PREM")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:PREM", "D-9")
    assert w.store.worker_claim_lease(w.p("system"), "demo:PREM")["epoch"] == 1


def test_ready_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    groups = w.store._ready_groups(project="demo")
    assert [i["key"] for i in groups["items"]] != ["demo:A"] or groups["needs_reconcile"]
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    groups = w.store._ready_groups(project="demo")
    assert "demo:A" in [i["key"] for i in groups["items"]]
    assert "demo:A" not in [i["key"] for i in groups["needs_reconcile"]]


def test_reconfirm_refuses_an_unknown_decision_a_bad_id_and_a_stranger(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "D-77777")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("l1other"), "demo:A", "D-5900")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "not-a-decision")
    assert _newer(w, "demo:A") == ["D-5900"]


def test_the_cli_and_the_api_carry_the_verb():
    from plangraph.keeper import api_routes, cli
    assert "reconfirm" in api_routes.WRITE_VERBS and "reconfirm" in api_routes.ROLE_CHECKED_VERBS
    args = cli.build_parser().parse_args(["reconfirm", "demo:A", "D-5900"])
    assert (args.key, args.decision) == ("demo:A", "D-5900")
