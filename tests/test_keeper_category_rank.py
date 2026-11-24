"""ITEM-CATEGORY-RANKED: items carry a category; a ranked category list per
project orders ready (and the feeder and the web table) before priority."""
from __future__ import annotations

import pytest

from keeper_util import World, defn, to_done
from plangraph.keeper.errors import Refused

START = ["speed", "efficiency-tokens", "efficiency-quality", "bug-fix", "reliability", "telemetry",
         "security", "network", "planning", "ui-ux", "feature", "cleanup"]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def _seed(w, **cats):
    items = [defn(k, priority=p, **({"category": c} if c else {}))
             for k, (p, c) in cats.items()]
    w.store.create_items(w.p("router"), items + [defn("GOAL", list(cats), kind="goal")])


def _order(w, project="demo"):
    return [r["key"] for r in w.store._ready_items(project=project, n=None)]


def test_the_start_list_is_data_in_the_keeper_in_rank_order(w):
    got = w.store.categories(w.p("reader"))
    assert [c["name"] for c in got["categories"]] == START
    assert got["uncategorised"] == 0


def test_ready_puts_a_speed_item_of_priority_8_before_an_efficiency_item_of_priority_10(w):
    _seed(w, EFF=(10, "efficiency-tokens"), SPEED=(8, "speed"), NONE=(10, None))
    assert _order(w) == ["demo:SPEED", "demo:EFF", "demo:NONE"]
    rows = {r["key"]: r for r in w.store._ready_items(project="demo", n=None)}
    assert rows["demo:SPEED"]["category"] == "speed" and rows["demo:SPEED"]["category_rank"] == 0
    assert rows["demo:NONE"]["category_rank"] == len(START)     # none sorts last


def test_changing_the_rank_list_changes_the_order(w):
    _seed(w, EFF=(10, "efficiency-tokens"), SPEED=(8, "speed"))
    w.store.rank_categories(w.p("router"), "demo", ["efficiency-tokens", "speed"])
    assert _order(w) == ["demo:EFF", "demo:SPEED"]
    # another project keeps the default order
    assert [c["name"] for c in w.store.categories(w.p("reader"), "other")["categories"]] == START
    assert [c["name"] for c in w.store.categories(w.p("reader"), "demo")["categories"]][:2] == [
        "efficiency-tokens", "speed"]


def test_a_new_category_is_added_by_one_verb_without_a_code_change(w):
    w.store.add_category(w.p("router"), "docs")
    assert w.store.categories(w.p("reader"))["categories"][-1] == {"name": "docs", "rank": len(START)}
    _seed(w, D=(5, "docs"), F=(5, "feature"))
    assert _order(w) == ["demo:F", "demo:D"]
    with pytest.raises(Refused):
        w.store.add_category(w.p("router"), "docs")
    with pytest.raises(Refused):
        w.store.add_category(w.p("l1main"), "other")
    with pytest.raises(Refused):
        w.store.add_category(w.p("router"), "Bad Name!")


def test_an_unknown_category_is_refused_naming_the_list(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("A", category="nope")])
    assert "speed" in exc.value.msg


def test_category_is_editable_and_uncategorised_items_are_counted(w):
    _seed(w, A=(1, None), B=(1, "speed"))
    assert w.store.categories(w.p("reader"))["uncategorised"] == 2     # A and the goal
    w.store.edit_definition(w.p("router"), "demo:A", {"category": "cleanup"})
    assert w.store.show(w.p("router"), "demo:A")["category"] == "cleanup"
    assert w.store.categories(w.p("reader"))["uncategorised"] == 1
    w.store.edit_definition(w.p("router"), "demo:A", {"category": None})
    assert w.store.categories(w.p("reader"))["uncategorised"] == 2


def test_the_snapshot_carries_the_category_and_the_ranked_list(w):
    _seed(w, A=(1, "speed"), B=(1, None))
    snap = w.store._snapshot(w.clock())
    items = {i["key"]: i for i in snap["items"]}
    assert items["demo:A"]["category"] == "speed" and "category" not in items["demo:B"]
    assert [c["name"] for c in snap["categories"]][:2] == ["speed", "efficiency-tokens"]
    assert snap["uncategorised"] == 2     # B and the goal


def test_backfill_by_key_rules_reports_the_rest(w):
    w.store.create_items(w.p("router"), [
        defn("SPEED-CACHE"), defn("TOKEN-SAVINGS-X"), defn("SECURITY-AUDIT"),
        defn("RANDOM-THING"), defn("GOAL", ["SPEED-CACHE", "TOKEN-SAVINGS-X", "SECURITY-AUDIT",
                                              "RANDOM-THING"], kind="goal")])
    dry = w.store.backfill_categories(w.p("router"), "demo", dry_run=True)
    assert dry["set"] == {"demo:SPEED-CACHE": "speed", "demo:TOKEN-SAVINGS-X": "efficiency-tokens",
                          "demo:SECURITY-AUDIT": "security"}
    assert "demo:RANDOM-THING" in dry["unmatched"]
    assert w.store.show(w.p("router"), "demo:SPEED-CACHE").get("category") is None
    w.store.backfill_categories(w.p("router"), "demo")
    assert w.store.show(w.p("router"), "demo:SPEED-CACHE")["category"] == "speed"


def test_reap_records_the_per_category_experiment_counts_and_alerts_a_starved_category(w):
    _seed(w, A=(5, "speed"), B=(5, "security"))
    w.store.reap(w.p("system"))
    metrics = {r["metric"]: r["value"] for r in w.conn.execute(
        "SELECT metric, value FROM metrics WHERE metric LIKE 'category_%'")}
    assert metrics["category_ready.speed"] == 1.0 and metrics["category_ready.security"] == 1.0
    assert not any(f["code"] == "category_starved" for f in w.store.alerts(w.p("router"))["findings"])
    w.clock.advance(49 * 3600)
    w.store.reap(w.p("system"))
    found = {f["key"]: f["msg"] for f in w.store.alerts(w.p("router"))["findings"]
             if f["code"] == "category_starved"}
    assert "category speed" in found["demo:A"] and "category security" in found["demo:B"]


def test_an_uncategorised_snapshot_still_carries_the_list_and_the_count(w):
    _seed(w, A=(1, None))
    snap = w.store._snapshot(w.clock())
    assert [c["name"] for c in snap["categories"]] == START and snap["uncategorised"] == 2


def test_the_snapshot_names_a_project_that_ranks_its_own_list(w):
    _seed(w, A=(1, "speed"))
    assert "category_overrides" not in w.store._snapshot(w.clock())
    w.store.rank_categories(w.p("router"), "demo", ["cleanup"])
    snap = w.store._snapshot(w.clock())
    assert snap["category_overrides"]["demo"][:2] == ["cleanup", "speed"]
    assert [c["name"] for c in snap["categories"]][:2] == ["speed", "efficiency-tokens"]   # the default list


def test_a_category_added_later_is_available_in_a_project_with_its_own_order(w):
    w.store.rank_categories(w.p("router"), "demo", ["cleanup", "speed"])
    w.store.add_category(w.p("router"), "docs")
    names = [c["name"] for c in w.store.categories(w.p("reader"), "demo")["categories"]]
    assert names[:2] == ["cleanup", "speed"] and names[-1] == "docs" and len(names) == len(START) + 1
    _seed(w, D=(5, "docs"), S=(5, "speed"))                       # define with it is accepted
    assert _order(w) == ["demo:S", "demo:D"]


def test_backfill_names_every_item_and_does_not_half_apply_over_a_claimed_one(w):
    w.store.create_items(w.p("router"), [
        defn("SPEED-A"), defn("SPEED-B"), defn("BUG-C"),
        defn("GOAL", ["SPEED-A", "SPEED-B", "BUG-C"], kind="goal")])
    w.store.claim(w.p("l1main"), "demo:SPEED-B")
    got = w.store.backfill_categories(w.p("router"), "demo")
    assert got["set"] == {"demo:SPEED-A": "speed", "demo:BUG-C": "bug-fix"}
    assert got["skipped"] == {"demo:SPEED-B": "claimed"} and got["failed"] == {}
    assert w.store.show(w.p("router"), "demo:SPEED-A")["category"] == "speed"
    assert w.store.show(w.p("router"), "demo:SPEED-B").get("category") is None


def test_telemetry_writes_zeros_counts_verified_merges_and_measures_the_ready_wait(w):
    w.store.create_items(w.p("router"), [defn("A", category="speed"),
                                         defn("B", ["A"], category="security"),
                                         defn("GOAL", ["B"], kind="goal")])
    w.clock.advance(100 * 3600)
    to_done(w, "demo:A")                         # B becomes ready only now
    w.store.reap(w.p("system"))
    metrics = {r["metric"]: r["value"] for r in w.conn.execute(
        "SELECT metric, value FROM metrics WHERE metric LIKE 'category_%'")}
    assert metrics["category_ready.security"] == 1.0 and metrics["category_ready.speed"] == 0.0
    assert metrics["category_ready.network"] == 0.0 and metrics["category_merged.speed"] == 0.0   # no verified merge
    found = [f for f in w.store.alerts(w.p("router"))["findings"] if f["code"] == "category_starved"]
    assert found == []      # B was created 100 h ago but has been ready for seconds
    w.clock.advance(49 * 3600)
    w.store.reap(w.p("system"))
    assert [f["key"] for f in w.store.alerts(w.p("router"))["findings"]
            if f["code"] == "category_starved"] == ["demo:B"]
    w.store.cancel(w.p("router"), "demo:B", "x")                 # the last ready item leaves
    w.store.reap(w.p("system"))
    assert w.conn.execute("SELECT value FROM metrics WHERE metric='category_ready.security'"
                          " ORDER BY computed_at DESC LIMIT 1").fetchone()[0] == 0.0
