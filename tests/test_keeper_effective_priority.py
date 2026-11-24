"""PRIORITY-INHERIT-EFFECTIVE on the keeper: ready rows, snapshot, telemetry."""
from __future__ import annotations

import pytest

from keeper_util import World, defn


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    router = world.p("router")
    world.store.create_items(router, [
        defn("LOW", priority=8), defn("OTHER", priority=9),
        defn("TOP", ["LOW"], priority=10),
        defn("GOAL", ["TOP", "OTHER"], kind="goal")])
    try:
        yield world
    finally:
        world.close()


def test_ready_rows_carry_the_effective_priority_and_its_source(w):
    rows = w.store._ready_items(project="demo", n=None)
    assert [r["key"] for r in rows] == ["demo:LOW", "demo:OTHER"]
    low = rows[0]
    assert (low["priority"], low["effective_priority"], low["priority_via"]) == (8, 10, "demo:TOP")
    assert "priority_via" not in rows[1] and rows[1]["effective_priority"] == 9
    assert "prio 8->10 via demo:TOP" in w.store.ready(w.p("router"), project="demo")


def test_closing_the_dependent_returns_the_blocker_to_its_own_value(w):
    w.store.cancel(w.p("router"), "demo:TOP", "not needed")
    low = next(r for r in w.store._ready_items(project="demo", n=None) if r["key"] == "demo:LOW")
    assert low["effective_priority"] == 8 and "priority_via" not in low


def test_the_snapshot_shows_both_values(w):
    items = {i["key"]: i for i in w.store._snapshot(w.clock())["items"]}
    assert items["demo:LOW"]["priority"] == 8
    assert items["demo:LOW"]["effective_priority"] == 10
    assert items["demo:LOW"]["priority_via"] == "demo:TOP"
    assert "effective_priority" not in items["demo:TOP"]   # only raised items carry it


def test_reap_records_the_daily_inheritance_telemetry(w):
    w.store.reap(w.p("system"))
    rows = {r["metric"]: r["value"] for r in w.conn.execute(
        "SELECT metric, value FROM metrics WHERE metric LIKE 'priority_inherit%'")}
    assert rows == {"priority_inherit_raised": 1.0, "priority_inherit_longest_chain": 1.0}


def test_aging_acts_on_the_own_value_only(w):
    w.clock.advance(8 * 86400)
    w.store.reap(w.p("system"))
    assert w.store.show(w.p("router"), "demo:LOW")["priority"] == 8

