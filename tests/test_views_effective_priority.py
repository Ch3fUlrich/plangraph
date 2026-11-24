"""PRIORITY-INHERIT-FROM-BLOCKED: an item that blocks a higher-priority item is
ordered at that priority; the stored priority never changes."""
from __future__ import annotations

from plangraph import views


def _it(key, prio, deps=(), state="open"):
    return {"key": f"d:{key}", "project": "d", "priority": prio, "state": state,
            "depends_on": [f"d:{k}" for k in deps], "estimate_h": 1}


def test_a_blocker_takes_the_priority_of_the_item_that_depends_on_it():
    items = [_it("LOW", 8), _it("TOP", 10, ["LOW"])]
    eff, cycles = views.effective_priorities(items)
    assert eff["d:LOW"] == {"effective": 10, "via": "d:TOP", "hops": 1}
    assert eff["d:TOP"]["effective"] == 10 and eff["d:TOP"]["via"] is None
    assert cycles == []
    assert items[0]["priority"] == 8          # the stored value is untouched


def test_inheritance_is_transitive_and_the_closed_dependent_returns_it():
    items = [_it("A", 1), _it("B", 2, ["A"]), _it("TOP", 10, ["B"])]
    eff, _ = views.effective_priorities(items)
    assert eff["d:A"] == {"effective": 10, "via": "d:TOP", "hops": 2}
    assert eff["d:B"] == {"effective": 10, "via": "d:TOP", "hops": 1}
    items[2]["state"] = "done"
    eff, _ = views.effective_priorities(items)
    assert eff["d:A"]["effective"] == 2 and eff["d:B"]["effective"] == 2


def test_ready_sorts_by_the_effective_value():
    items = [_it("LOW", 8), _it("TOP", 10, ["LOW"]), _it("MID", 9)]
    assert [i["key"] for i in views.ready(items)] == ["d:LOW", "d:MID"]


def test_a_cycle_is_reported_not_looped():
    items = [_it("A", 3, ["B"]), _it("B", 7, ["A"])]
    eff, cycles = views.effective_priorities(items)
    assert cycles and sorted(cycles[0]) == ["d:A", "d:B"]
    assert eff["d:A"]["effective"] in (3, 7) and eff["d:B"]["effective"] in (3, 7)


def test_the_highest_dependent_wins_and_ties_keep_the_smallest_key():
    items = [_it("X", 1), _it("P9", 9, ["X"]), _it("P10B", 10, ["X"]), _it("P10A", 10, ["X"])]
    eff, _ = views.effective_priorities(items)
    assert eff["d:X"]["effective"] == 10 and eff["d:X"]["via"] == "d:P10A"


def test_inheritance_stats_count_raised_items_and_the_longest_chain():
    items = [_it("A", 1), _it("B", 2, ["A"]), _it("TOP", 10, ["B"]), _it("FREE", 5)]
    eff, _ = views.effective_priorities(items)
    assert views.inheritance_stats(eff) == {"raised": 2, "longest_chain": 2}


def test_an_item_without_a_category_rank_sorts_after_every_ranked_one():
    top = {**_it("TOP", 1), "category_rank": 0}
    last = {**_it("RANKED", 1), "category_rank": 7}
    none = _it("NONE", 10)          # no rank key at all: the offline snapshot door
    assert [i["key"] for i in views.ready([none, last, top])] == ["d:TOP", "d:RANKED", "d:NONE"]


def test_equal_rank_and_priority_rows_are_ordered_by_age_before_unblocks():
    old = {**_it("OLD", 5), "created_at": "2026-01-01T00:00:00Z"}
    new = {**_it("NEW", 5), "created_at": "2026-06-01T00:00:00Z"}
    blocker = _it("TOP", 1, ["NEW"])          # NEW unblocks more, yet OLD is older
    undated = _it("UNDATED", 5)
    assert [i["key"] for i in views.ready([undated, new, old, blocker])][:3] == ["d:OLD", "d:NEW", "d:UNDATED"]


def test_a_ci_fix_and_a_live_pr_sort_before_a_speed_item_as_in_the_feeder():
    speed = {**_it("SPEED", 1), "category_rank": 0}
    ci = {**_it("BUILD-CI-FIX", 1), "category_rank": 8}
    pr = {**_it("CARRIED", 1), "category_rank": 9, "note": "pr: 123"}
    closed = {**_it("MERGED-PR", 1), "category_rank": 9, "note": "pr: 7\npr_state: merged"}
    assert [views.work_class(i) for i in (pr, ci, speed, closed)] == [0, 1, 2, 2]
    got = [i["key"] for i in views.ready([speed, closed, ci, pr])]
    assert got == ["d:CARRIED", "d:BUILD-CI-FIX", "d:SPEED", "d:MERGED-PR"]


def test_a_merged_pr_object_in_the_note_is_not_a_live_pr_like_in_the_feeder():
    merged = {**_it("OBJ-MERGED", 1), "note": 'pr: {"number": 7, "state": "merged"}'}
    live = {**_it("OBJ-LIVE", 1), "note": 'pr: {"number": 8, "state": "open"}'}
    flagged = {**_it("OBJ-FLAG", 1), "note": 'pr: {"number": 9, "merged": true}'}
    quoted = {**_it("QUOTED", 1), "note": 'pr: 12\npr_state: "closed"'}
    fix = {**_it("PLAIN", 1), "note": "ci_fix: true"}
    assert [views.work_class(i) for i in (merged, live, flagged, quoted, fix)] == [2, 0, 2, 2, 1]
