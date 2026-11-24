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



def test_the_highest_dependent_wins_and_ties_keep_the_smallest_key():
    items = [_it("X", 1), _it("P9", 9, ["X"]), _it("P10B", 10, ["X"]), _it("P10A", 10, ["X"])]
    eff, _ = views.effective_priorities(items)
    assert eff["d:X"]["effective"] == 10 and eff["d:X"]["via"] == "d:P10A"


def test_inheritance_stats_count_raised_items_and_the_longest_chain():
    items = [_it("A", 1), _it("B", 2, ["A"]), _it("TOP", 10, ["B"]), _it("FREE", 5)]
    eff, _ = views.effective_priorities(items)
    assert views.inheritance_stats(eff) == {"raised": 2, "longest_chain": 2}


def test_brief_shows_the_source_of_a_raised_priority():
    items = [_it("LOW", 8), _it("TOP", 10, ["LOW"])]
    assert "prio 8->10 via d:TOP" in views.brief(items)
