"""Active item definition edit epoch bumping tests.

When the router edits done_criteria or depends_on on a CLAIMED or IN_REVIEW item
via edit_definition, the item's epoch is incremented by 1 in the same UPDATE.
This bump voids old evidence and review verdicts collected under outdated
criteria or dependencies, satisfying the F4 coordinator decision.
"""
from __future__ import annotations

import pytest
from keeper_util import World, seed, to_claimed, to_in_review
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused

KEY = "demo:A"
RATIONALE = {"criteria_reason": "Cover the retry path", "criteria_evidence": "D-123"}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    yield world
    world.close()


def last_defined(w, key: str = KEY) -> dict:
    events = [
        e for e in keeper_events.read_events(w.conn)
        if e.get("type") == "item_defined" and e.get("key") == key
    ]
    assert events, f"no item_defined event found for {key}"
    return events[-1]


def folded(w, key: str = KEY) -> dict:
    return keeper_events.fold(keeper_events.read_events(w.conn))[key]


def test_done_criteria_edit_bumps_epoch_on_claimed_item(w):
    epoch = to_claimed(w, KEY)
    before = w.store._item(KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {**RATIONALE, "done_criteria": "tighter criteria"})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert item["epoch"] == epoch + 1
    assert item["state"] == "claimed"
    assert item["claimed_by"] == before["claimed_by"] == "l1main"
    assert item["lease_until"] == before["lease_until"]
    assert item["done_criteria"] == "tighter criteria"


def test_depends_on_edit_bumps_epoch_on_claimed_item(w):
    epoch = to_claimed(w, KEY)
    before = w.store._item(KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {"depends_on": ["demo:B"]})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert item["epoch"] == epoch + 1
    assert item["state"] == "claimed"
    assert item["depends_on"] == ["demo:B"]
    assert item["claimed_by"] == before["claimed_by"] == "l1main"
    assert item["lease_until"] == before["lease_until"]


def test_done_criteria_edit_bumps_epoch_on_in_review_item(w):
    epoch = to_in_review(w, KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {**RATIONALE, "done_criteria": "revised criteria"})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert item["epoch"] == epoch + 1
    assert item["state"] == "in_review"
    assert item["done_criteria"] == "revised criteria"


def test_tier_only_edit_does_not_bump_epoch(w):
    epoch_a = to_claimed(w, KEY)
    out_a = w.store.edit_definition(w.p("router"), KEY,
                                    {"review_tier": "high"})
    assert out_a == {"key": KEY}
    item_a = w.store._item(KEY)
    assert item_a["epoch"] == epoch_a
    assert item_a["round"] == 1
    event_a = last_defined(w, KEY)
    assert "new_epoch" not in event_a["payload"]

    key_b = "demo:B"
    epoch_b = to_claimed(w, key_b)
    out_b = w.store.edit_definition(w.p("router"), key_b,
                                    {"evidence_kinds": ["decision", "any"]})
    assert out_b == {"key": key_b}
    item_b = w.store._item(key_b)
    assert item_b["epoch"] == epoch_b
    assert item_b["round"] == 1
    event_b = last_defined(w, key_b)
    assert "new_epoch" not in event_b["payload"]


def test_evidence_before_the_edit_stops_counting(w):
    epoch = to_claimed(w, KEY)
    w.store.add_evidence(w.p("l1main"), KEY, epoch, "decision", {"id": "D-1"})
    w.store.edit_definition(w.p("router"), KEY,
                            {**RATIONALE, "done_criteria": "tighter criteria"})
    new_epoch = w.store._item(KEY)["epoch"]
    assert new_epoch == epoch + 1

    with pytest.raises(Refused) as exc_info:
        w.store.submit(w.p("l1main"), KEY, new_epoch)
    assert exc_info.value.code == "evidence_missing"

    w.store.add_evidence(w.p("l1main"), KEY, new_epoch, "decision", {"id": "D-2"})
    w.store.submit(w.p("l1main"), KEY, new_epoch)
    item = w.store._item(KEY)
    assert item["state"] == "in_review"


def test_fold_equals_cache_after_the_edit(w):
    epoch = to_claimed(w, KEY)
    w.store.edit_definition(w.p("router"), KEY,
                            {**RATIONALE, "done_criteria": "tighter criteria"})
    w.store.edit_definition(w.p("router"), KEY,
                            {"depends_on": ["demo:B"]})
    item = w.store._item(KEY)
    cache_epoch = item["epoch"]
    assert cache_epoch == epoch + 2

    folded_item = folded(w, KEY)
    assert folded_item["epoch"] == cache_epoch == epoch + 2
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []


def test_event_carries_who_before_after_and_new_epoch(w):
    epoch = to_claimed(w, KEY)
    w.store.edit_definition(w.p("router"), KEY,
                            {**RATIONALE, "done_criteria": "newer"})
    event = last_defined(w, KEY)
    payload = event["payload"]
    assert payload["who"] == "router"
    assert payload["before"] == {"done_criteria": "criteria"}
    assert payload["after"] == {"done_criteria": "newer"}
    assert payload["new_epoch"] == epoch + 1
    assert payload["while"] == "claimed"


def test_claimants_stale_epoch_is_refused_after_the_edit(w):
    epoch = to_claimed(w, KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {**RATIONALE, "done_criteria": "tighter criteria"})
    new_epoch = out["epoch"]

    with pytest.raises(Refused) as exc_info:
        w.store.heartbeat(w.p("l1main"), KEY, epoch)
    assert exc_info.value.code == "epoch_stale"

    with pytest.raises(Refused) as exc_info:
        w.store.add_evidence(w.p("l1main"), KEY, epoch, "decision", {"id": "D-1"})
    assert exc_info.value.code == "epoch_stale"

    w.store.heartbeat(w.p("l1main"), KEY, new_epoch)
    assert w.store._item(KEY)["state"] == "claimed"


@pytest.mark.parametrize("active", [False, True])
def test_criteria_amendments_are_append_only(w, active):
    from plangraph.keeper import db_errors

    if active:
        to_claimed(w, KEY)
    fields = {
        "done_criteria": "revised criteria",
        "criteria_reason": "Cover the retry path",
        "criteria_evidence": "D-123",
    }
    for _ in range(2):
        w.store.edit_definition(w.p("router"), KEY, fields)
    rows = w.conn.execute(
        "SELECT original_text, new_text, reason, evidence, authority, ts "
        "FROM criteria_amendments WHERE key=? ORDER BY seq", (KEY,)).fetchall()
    assert [tuple(row) for row in rows] == [
        ("criteria", "revised criteria", "Cover the retry path",
         "D-123", "router", w.store._iso(w.clock())),
    ]
    for sql in (
        "UPDATE criteria_amendments SET reason='rewritten' WHERE key=?",
        "DELETE FROM criteria_amendments WHERE key=?",
    ):
        with pytest.raises(db_errors.INTEGRITY, match="criteria amendments are append-only"):
            w.conn.execute(sql, (KEY,))


@pytest.mark.parametrize("active", [False, True])
def test_verify_reports_a_missing_criteria_amendment(w, monkeypatch, active):
    if active:
        to_claimed(w, KEY)
    with monkeypatch.context() as broken:
        broken.setattr(w.store, "_record_criteria_amendment", lambda *_: None)
        w.store.edit_definition(w.p("router"), KEY, {
            "done_criteria": "revised criteria",
            "criteria_reason": "Cover the retry path",
            "criteria_evidence": "D-123",
        })
    missing = last_defined(w)["seq"]
    w.store.edit_definition(w.p("router"), KEY, {
        "done_criteria": "final criteria",
        "criteria_reason": "Cover the recovery path",
        "criteria_evidence": "D-124",
    })
    assert [(p["key"], p["seq"], p["code"]) for p in w.store.verify_integrity()] == [
        (KEY, missing, "criteria_amendment_missing"),
    ]


def test_verify_reports_legacy_criteria_edits_without_inventing_text(w):
    with w.store._write():
        w.conn.execute("UPDATE items SET done_criteria=? WHERE key=?", ("legacy edit", KEY))
        seq, _ = w.store._append(w.p("router"), KEY, "item_defined",
                                 {"fields": ["done_criteria"]}, w.clock())
    w.store.edit_definition(w.p("router"), KEY, {**RATIONALE, "done_criteria": "current"})
    assert [(p["key"], p["seq"], p["code"]) for p in w.store.verify_integrity()] == [
        (KEY, seq, "criteria_amendment_unverifiable"),
    ]
    assert w.store.verify_chain() == []


def test_verify_reports_criteria_text_that_disagrees_with_the_event(w, monkeypatch):
    record = w.store._record_criteria_amendment

    def record_wrong_original(seq, principal, item, fields, amendment, now):
        record(seq, principal, {**item, "done_criteria": "wrong"}, fields, amendment, now)

    monkeypatch.setattr(w.store, "_record_criteria_amendment", record_wrong_original)
    w.store.edit_definition(w.p("router"), KEY, {**RATIONALE, "done_criteria": "current"})
    assert w.store.verify_integrity() == [{
        "key": KEY, "seq": last_defined(w)["seq"], "code": "criteria_amendment_mismatch",
        "field": "done_criteria", "expected": ("criteria", "current"), "got": ("wrong", "current"),
    }]


@pytest.mark.parametrize("metadata", [{}, {"criteria_reason": "Cover retries"},
                                     {"criteria_evidence": "D-123"}])
def test_criteria_change_requires_reason_and_evidence(w, metadata):
    before = keeper_events.read_events(w.conn)
    with pytest.raises(Refused, match="criteria_(reason|evidence)") as exc:
        w.store.edit_definition(w.p("router"), KEY, {"done_criteria": "new", **metadata})
    assert exc.value.code == "validation"
    assert w.store._item(KEY)["done_criteria"] == "criteria"
    assert keeper_events.read_events(w.conn) == before
    assert w.conn.execute("SELECT COUNT(*) FROM criteria_amendments").fetchone()[0] == 0


def test_unchanged_criteria_need_no_amendment(w):
    w.store.edit_definition(w.p("router"), KEY, {"done_criteria": "criteria"})
    assert w.conn.execute("SELECT COUNT(*) FROM criteria_amendments").fetchone()[0] == 0
    assert w.store.verify_integrity() == []
