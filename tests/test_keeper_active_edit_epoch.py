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
                                  {"done_criteria": "tighter criteria"})
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
                                  {"done_criteria": "revised criteria"})
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
                                    {"evidence_kinds": ["decision"]})
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
                            {"done_criteria": "tighter criteria"})
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
                            {"done_criteria": "tighter criteria"})
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
                            {"done_criteria": "newer"})
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
                                  {"done_criteria": "tighter criteria"})
    new_epoch = out["epoch"]

    with pytest.raises(Refused) as exc_info:
        w.store.heartbeat(w.p("l1main"), KEY, epoch)
    assert exc_info.value.code == "epoch_stale"

    with pytest.raises(Refused) as exc_info:
        w.store.add_evidence(w.p("l1main"), KEY, epoch, "decision", {"id": "D-1"})
    assert exc_info.value.code == "epoch_stale"

    w.store.heartbeat(w.p("l1main"), KEY, new_epoch)
    assert w.store._item(KEY)["state"] == "claimed"
