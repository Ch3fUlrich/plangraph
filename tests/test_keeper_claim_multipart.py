"""Merged parts do not finish an item while a named lane remains open."""
import pytest

from test_keeper_claim_guard_uncached import scenario
from plangraph.keeper import context_sources
from plangraph.keeper.errors import Refused


MERGED = ("already merged: Plan-Item commit abcdef12 (2026-11-06) via PR #42"
          " is on the default branch; attach evidence/submit instead")


@pytest.fixture
def multipart(scenario, monkeypatch):
    world, path, _ = scenario
    monkeypatch.setattr(context_sources, "plan_item_commits", lambda *a, **kw: [
        {"sha": "abcdef123456", "date": "2026-11-06", "merge": {"pr": 42}}])
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: [])
    return world, path


def test_open_part_allows_claim(multipart, monkeypatch):
    world, _ = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: ["lane/a-part2"])
    item = world.store._item("demo:A")
    before = world.store._task_context(item, {"demo:A": item})
    assert "already merged abcdef12" in before["stale"]
    world.store.claim(world.p("l1main"), "demo:A")
    assert world.store._item("demo:A")["state"] == "claimed"


@pytest.mark.parametrize("lanes", [[], None], ids=["no-open-part", "unknown-lanes"])
def test_merged_without_known_open_part_refuses(multipart, monkeypatch, lanes):
    world, _ = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: lanes)
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A")
    assert exc.value.code == "bad_state"
    assert str(exc.value) == ("bad_state: demo:A stale: " + MERGED
                             + " (override: force_stale=<reason>, audited)")
    assert world.store._item("demo:A")["state"] == "open"


def test_open_part_still_refuses_newer_decision(multipart, monkeypatch):
    world, path = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: ["lane/a-part2"])
    path.write_text(path.read_text().replace("demo:B", "demo:A"))
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A")
    assert exc.value.code == "bad_state"
    assert "premise outdated: D-9" in str(exc.value)
    assert "already merged" not in str(exc.value)
    assert world.store._item("demo:A")["state"] == "open"


def test_force_stale_without_open_lane(multipart):
    world, _ = multipart
    item = world.store._item("demo:A")
    assert world.store._claim_guard(item, " checked ") == {
        "reason": "checked", "overrode": [MERGED]}
    world.store.claim(world.p("l1main"), "demo:A", force_stale="checked")
    assert world.store._item("demo:A")["state"] == "claimed"
