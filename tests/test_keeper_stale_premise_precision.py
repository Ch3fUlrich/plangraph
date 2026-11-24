"""KEEPER-STALE-PREMISE-PRECISION: the premise gate ignores the decision that
defines or orders the item, and one verb re-confirms a premise.

Decision rows carry the ``(HH:MMZ`` stamp of the decisions log; the log has
neither a date nor a session, so a time of day proves nothing.
"""
from __future__ import annotations

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper import events as keeper_events
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


def test_the_cases_of_2026_11_13_claim_without_force(w, decisions):
    _items(w, PROD={"source": "D-5862"})
    # (a) the item's own source decision
    _decide(decisions, 5862, "(05:0xZ) PROD-FROM-MAIN-ONLY discussion of the rollout")
    # (b) rows that order, GO or define the item without changing scope
    _decide(decisions, 5858, "(05:0xZ) OPERATOR order: demo:SPAWNER-DEFAULT ordered first, GO")
    _decide(decisions, 5837, "(23:4xZ) GO: demo:PHASE-LINE-GREEN defined and queued")
    _decide(decisions, 5853, "(00:1xZ) lesson items: demo:LESSON-ITEM queued in the plan")
    assert _claims(w, "demo:PROD-FROM-MAIN-ONLY")
    assert _claims(w, "demo:SPAWNER-DEFAULT")
    assert _claims(w, "demo:PHASE-LINE-GREEN")
    assert _claims(w, "demo:LESSON-ITEM")


def test_a_row_near_the_define_in_time_of_day_is_not_trusted_without_session_and_date(w, decisions):
    _items(w)
    # the log has neither: a stamp inside two hours of the define could be another day and session
    _decide(decisions, 5860, "(23:4xZ) note on demo:SCOPE-ITEM wording")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")
    assert "premise outdated: D-5860" in exc.value.msg
    # a later scope change after an ordering row still refuses
    _decide(decisions, 5861, "(01:0xZ) demo:SPAWNER-DEFAULT ordered first")
    _decide(decisions, 5862, "(07:0xZ) demo:SPAWNER-DEFAULT now covers the web too")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:SPAWNER-DEFAULT")
    assert "D-5862" in exc.value.msg


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
             if e["key"] == "demo:SCOPE-ITEM" and e["type"] == "premise_reconfirmed"
             and e["payload"].get("decision") == "D-5900"]
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


def test_reconfirm_is_not_a_definition_change_and_does_not_clear_a_supersedes_link(w, decisions):
    _items(w)
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM premise changed")
    before = [e for e in keeper_events.read_events(w.conn) if e["type"] == "item_defined"]
    w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900")
    assert [e for e in keeper_events.read_events(w.conn) if e["type"] == "item_defined"] == before
    assert w.store.verify_integrity() == []
    w.store.create_items(w.p("router"), [defn("NEWER", supersedes=["demo:SCOPE-ITEM"], under="demo:GOAL")])
    with pytest.raises(Refused) as exc:
        w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900")
    assert "supersession=KEY" in exc.value.msg
    with pytest.raises(Refused):
        w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")


def test_force_stale_claims_count_every_audited_claim_even_after_the_item_closes(w, decisions):
    _items(w)
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM premise changed")
    key = "demo:SCOPE-ITEM"
    w.store.claim(w.p("l1main"), key, force_stale="read D-5900")
    w.store.release(w.p("l1main"), key, 1)
    w.store.claim(w.p("l1main"), key, force_stale="read D-5900 again")       # same item, second claim
    assert w.store.alerts(w.p("router"))["force_stale_claims"] == 2
    w.store.cancel(w.p("router"), key, "closed")
    assert w.store.alerts(w.p("router"))["force_stale_claims"] == 2


# ------------------------------------------------------------ review round 3
def test_system_claims_refuse_a_retired_premise_and_a_superseded_contract(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("PREM", description="per D-5"),
        defn("CONT", title="Plan v1 work", description="Plan v2 supersedes the earlier text. Plan v2",
             done_criteria="Plan v1 done"),
        defn("GOAL", ["PREM", "CONT"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    for key in ("demo:PREM", "demo:CONT"):
        found = w.store.show(w.p("router"), key)
        assert found["stale"], found
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:PREM")
    assert "premise D-5 superseded by D-9" in exc.value.msg


def test_a_go_row_that_adds_a_second_approval_requirement_is_not_neutral(w, decisions):
    _items(w)
    _decide(decisions, 5870, "(05:0xZ) GO demo:SPAWNER-DEFAULT, but it also needs a second approval first")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:SPAWNER-DEFAULT")
    assert "premise outdated: D-5870" in exc.value.msg


def test_reconfirm_clears_an_explicit_supersession_it_names_and_is_audited(w, decisions):
    _items(w)
    w.store.create_items(w.p("router"), [defn("NEWER", supersedes=["demo:SCOPE-ITEM"], under="demo:GOAL")])
    _decide(decisions, 5900, "(05:0xZ) demo:SCOPE-ITEM re-read after NEWER was defined")
    with pytest.raises(Refused) as exc:                       # the superseder must be named
        w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900")
    assert "demo:NEWER" in exc.value.msg
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900", supersession="demo:OTHER")
    got = w.store.reconfirm(w.p("router"), "demo:SCOPE-ITEM", "D-5900", supersession="demo:NEWER")
    assert got["cleared_supersession"] == "demo:NEWER"
    assert w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")["epoch"] == 1
    marks = [e for e in keeper_events.read_events(w.conn)
             if e["type"] == "premise_reconfirmed" and e["payload"].get("supersession") == "demo:NEWER"]
    assert marks and marks[0]["actor"] == "router"
    # a different superseder defined later is not cleared by the old answer
    w.store.release(w.p("l1main"), "demo:SCOPE-ITEM", 1)
    w.store.create_items(w.p("router"), [defn("NEWEST", supersedes=["demo:SCOPE-ITEM"], under="demo:GOAL")])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:SCOPE-ITEM")
    assert "demo:NEWEST" in exc.value.msg and "demo:NEWER" not in exc.value.msg


def test_the_alerts_text_shows_the_force_stale_count(w, decisions):
    from plangraph.keeper import fmt
    assert "force_stale_claims 2" in fmt.alerts_text({"force_stale_claims": 2, "lease_expiring": [],
                                                      "review_overdue": [], "findings": [], "refusals": [],
                                                      "stalled": False, "warnings": []})
