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


def test_reap_records_the_telemetry_of_the_graph_after_aging(tmp_path):
    world = World(tmp_path)
    try:
        world.store.create_items(world.p("router"), [
            defn("BLOCKER", priority=9), defn("TOP", ["BLOCKER"], priority=10),
            defn("GOAL", ["TOP"], kind="goal")])
        world.clock.advance(4 * 86400)       # TOP (10, unclaimed 3 days) ages to 9; BLOCKER (9, 7 days) stays
        world.store.reap(world.p("system"))
        assert world.store.show(world.p("router"), "demo:TOP")["priority"] == 9
        rows = {r["metric"]: r["value"] for r in world.conn.execute(
            "SELECT metric, value FROM metrics WHERE metric LIKE 'priority_inherit%'")}
        assert rows == {"priority_inherit_raised": 0.0, "priority_inherit_longest_chain": 0.0}
    finally:
        world.close()


def test_reap_replaces_the_source_of_an_existing_metric_row(w):
    day = w.store._iso(w.clock())[:10]
    w.conn.execute("INSERT INTO metrics(day,metric,value,source,computed_at) VALUES(?,?,?,?,?)",
                   (day, "priority_inherit_raised", 99.0, "manual", "2020-01-01T00:00:00Z"))
    w.store.reap(w.p("system"))
    row = w.conn.execute("SELECT value, source FROM metrics WHERE day=? AND metric=?",
                         (day, "priority_inherit_raised")).fetchone()
    assert (row["value"], row["source"]) == (1.0, "reap")


def test_ready_and_snapshot_name_the_smallest_source_through_equal_priorities(tmp_path):
    world = World(tmp_path)
    try:
        world.store.create_items(world.p("router"), [
            defn("ROOT", priority=1), defn("M", ["ROOT"], priority=10),
            defn("Z", ["ROOT"], priority=10), defn("A", ["Z"], priority=10),
            defn("GOAL", ["M", "A"], kind="goal")])
        root = next(r for r in world.store._ready_items(project="demo", n=None) if r["key"] == "demo:ROOT")
        assert (root["effective_priority"], root["priority_via"]) == (10, "demo:A")
        items = {i["key"]: i for i in world.store._snapshot(world.clock())["items"]}
        assert items["demo:ROOT"]["priority_via"] == "demo:A"
        assert "priority_via" not in items["demo:Z"]
    finally:
        world.close()


def test_reap_records_the_longest_chain_when_two_routes_reach_one_source(tmp_path):
    world = World(tmp_path)
    try:
        world.store.create_items(world.p("router"), [
            defn("ROOT", priority=1), defn("MID", ["ROOT"], priority=2),
            defn("TOP", ["ROOT", "MID"], priority=10), defn("GOAL", ["TOP"], kind="goal")])
        world.store.reap(world.p("system"))
        rows = {r["metric"]: r["value"] for r in world.conn.execute(
            "SELECT metric, value FROM metrics WHERE metric LIKE 'priority_inherit%'")}
        assert rows == {"priority_inherit_raised": 2.0, "priority_inherit_longest_chain": 2.0}
    finally:
        world.close()
