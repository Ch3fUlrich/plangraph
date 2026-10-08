"""Keeper snapshot activity feed (W14, operator D-696).

Mutation that fails it: dropping ``review_passed`` from the allow-list
(e.g. filtering it out like ``evidence_added``) breaks
``test_activity_types_order_fields``.
"""
from __future__ import annotations

import pytest

from keeper_util import World, seed, to_claimed, to_done, to_in_review


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"]), ("C", ["A"])])
    yield world
    world.close()


def _activity(w):
    return w.store.snapshot(w.p("reader"))["activity"]


def test_activity_types_order_fields(w):
    to_done(w, "demo:A")  # claimed + submitted + review_passed
    to_in_review(w, "demo:B")  # claimed + submitted (+evidence_added, filtered)
    w.store.set_legacy_state(w.p("router"), "demo:C", "claimed")
    long_reason = "x" * 200
    w.store.cancel(w.p("router"), "demo:C", long_reason)
    act = _activity(w)
    by_key = {}
    for e in act:
        by_key.setdefault(e["key"], []).append(e["type"])
    assert "claimed" in by_key["demo:A"]
    assert "submitted" in by_key["demo:B"]
    assert "legacy_state_set" in by_key["demo:C"]
    assert "cancelled" in by_key["demo:C"]
    assert "review_passed" in by_key["demo:A"]
    assert "item_created" in [e["type"] for e in act
                              if e["key"] == "demo:GOAL"]
    types = {e["type"] for e in act}
    assert "evidence_added" not in types
    assert "item_defined" not in types
    # oldest first
    seqs = [r["seq"] for r in w.conn.execute(
        "SELECT seq FROM events WHERE type IN"
        " ('item_created','legacy_state_set','closed_on_decision','cancelled',"
        " 'claimed','submitted','review_passed','changes_requested')"
        " ORDER BY seq").fetchall()]
    assert len(act) <= len(seqs)
    tss = [e["ts"] for e in act]
    assert tss == sorted(tss)
    for e in act:
        assert set(e) <= {"ts", "key", "type", "actor", "state", "reason"}
        assert {"ts", "key", "type", "actor"} <= set(e)
        assert "hash" not in e and "payload" not in e
    leg = [e for e in act if e["type"] == "legacy_state_set"][0]
    assert leg["state"] == "claimed"
    canc = [e for e in act if e["type"] == "cancelled"][0]
    assert canc["reason"] == long_reason[:120] and len(canc["reason"]) <= 120
    assert "state" not in canc
    created = [e for e in act if e["type"] == "item_created"][0]
    assert created["state"] == "open"


def test_activity_changes_requested_state(w):
    to_in_review(w, "demo:A")
    w.store.verdict(w.p("reviewer"), "demo:A", False, "fix it")
    act = _activity(w)
    ch = [e for e in act if e["type"] == "changes_requested"]
    assert ch and ch[0]["state"] == "claimed"
    assert "reason" not in ch[0]


def test_activity_window(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])])
    old = _activity(w)
    assert old
    w.clock.advance(32 * 86400)
    w.store.claim(w.p("l1main"), "demo:A")
    act = _activity(w)
    assert act and all(e["type"] == "claimed" for e in act)
    w.close()


def test_activity_cap(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])])
    now = w.clock()
    for i in range(5100):
        w.store._append(w.p("router"), f"demo:K{i}", "claimed",
                        {"epoch": 1}, now)
        w.clock.advance(1)
    act = _activity(w)
    assert len(act) == 5000
    assert act[-1]["key"] == "demo:K5099"
    assert act[0]["key"] == "demo:K100"
    w.close()


def test_activity_never_raises(w):
    assert w.store._activity(None) == []
    assert w.store._activity("not-a-time") == []
