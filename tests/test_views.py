"""Tests for the Views API (docs/SPEC.md) — pure functions over item dicts."""
import json
from pathlib import Path

import pytest

from plangraph import views

FIX = Path(__file__).parent / "fixtures"


def load(name):
    """Load a snapshot fixture and return its item list."""
    return json.loads((FIX / name).read_text(encoding="utf-8"))["items"]


def item(key, state="open", deps=(), est=1.0, kind="lane", title=None, prio=0,
         project=None, corrects=None):
    """Build a minimal item dict for inline edge cases."""
    proj, _, iid = key.partition(":")
    return {
        "project": project or proj, "id": iid, "key": key,
        "title": title or f"title {iid}", "kind": kind, "track": "T0",
        "owner": "test", "state": state, "estimate_h": est,
        "depends_on": list(deps), "corrects": corrects,
        "done_criteria": "", "evidence_kinds": ["any"], "operator": False,
        "priority": prio,
    }


def codes(problems):
    return sorted(p["code"] for p in problems)


def keys(rows):
    return [r["key"] for r in rows]


# ---------------------------------------------------------------- index

def test_index_maps_key_to_item():
    items = load("diamond.json")
    idx = views.index(items)
    assert set(idx) == {"demo:A", "demo:B", "demo:C", "demo:GOAL"}
    assert idx["demo:B"]["estimate_h"] == 2


def test_index_duplicate_key_last_wins():
    idx = views.index(load("dup_key.json"))
    assert idx["demo:A"]["estimate_h"] == 3


# ---------------------------------------------------------------- validate

@pytest.mark.parametrize("name", ["diamond.json", "cross_project.json",
                                  "done_items.json", "mini_snapshot.json"])
def test_validate_clean_graphs_have_no_problems(name):
    assert views.validate(load(name)) == []


@pytest.mark.parametrize("name,expected", [
    ("dup_key.json", "dup_key"),
    ("cycle.json", "cycle"),
    ("cancelled_dep.json", "depends_on_cancelled"),
    ("no_goal.json", "no_goal"),
    ("unreachable.json", "unreachable_goal"),
    ("self_dep.json", "self_dep"),
    ("corrects_not_done.json", "corrects_not_done"),
])
def test_validate_flags_expected_code(name, expected):
    assert expected in codes(views.validate(load(name)))


def test_validate_bad_state():
    items = load("diamond.json")
    items[0]["state"] = "bogus"
    assert "bad_state" in codes(views.validate(items))


def test_validate_unknown_dep():
    items = [item("demo:A", deps=["demo:GHOST"]),
             item("demo:GOAL", kind="goal", deps=["demo:A"], est=0)]
    assert "unknown_dep" in codes(views.validate(items))


def test_validate_corrects_unknown_key():
    items = [item("demo:A"),
             item("demo:B", deps=["demo:A"], corrects="demo:ZZZ"),
             item("demo:GOAL", kind="goal", deps=["demo:B"], est=0)]
    assert "corrects_not_done" in codes(views.validate(items))


def test_validate_cycle_names_the_nodes():
    problems = [p for p in views.validate(load("cycle.json")) if p["code"] == "cycle"]
    assert problems, "cycle fixture must flag a cycle"
    msg = problems[0]["msg"]
    assert "demo:A" in msg and "demo:B" in msg


def test_validate_self_dep_is_not_double_reported_as_cycle():
    assert codes(views.validate(load("self_dep.json"))) == ["self_dep"]


def test_validate_done_and_cancelled_are_exempt_from_unreachable():
    items = [item("demo:GOAL", kind="goal", est=0),
             item("demo:DONE", state="done"),
             item("demo:CANC", state="cancelled")]
    assert views.validate(items) == []


def test_validate_no_goal_suppresses_unreachable_noise():
    assert codes(views.validate(load("no_goal.json"))) == ["no_goal"]


def test_validate_output_is_sorted_and_order_independent():
    items = load("mini_snapshot.json")
    assert views.validate(items) == views.validate(list(reversed(items)))


# ---------------------------------------------------------------- ready

def test_ready_only_open_items_with_all_deps_done():
    assert keys(views.ready(load("diamond.json"))) == ["demo:A"]
    assert keys(views.ready(load("done_items.json"))) == ["demo:B"]


def test_ready_excludes_claimed_in_review_and_terminal():
    ready = set(keys(views.ready(load("mini_snapshot.json"))))
    assert "borealis:A-4" not in ready   # claimed
    assert "borealis:A-5" not in ready   # in_review
    assert "borealis:A-1" not in ready   # done
    assert "borealis:A-8" not in ready   # cancelled


def test_ready_sort_is_priority_then_unblocks_then_estimate_then_key():
    items = [
        item("demo:X", prio=0, est=9),
        item("demo:Y", prio=5, est=9),
        item("demo:Z", prio=0, est=1),
        item("demo:GOAL", kind="goal", est=0, deps=["demo:X", "demo:Y", "demo:Z"]),
    ]
    assert keys(views.ready(items)) == ["demo:Y", "demo:Z", "demo:X"]


def test_ready_project_filter_keeps_cross_project_deps():
    cp = load("cross_project.json")
    assert keys(views.ready(cp, project="atlas")) == ["atlas:R1"]
    assert views.ready(cp, project="borealis") == []
    assert keys(views.ready(load("mini_snapshot.json"), project="atlas")) == ["atlas:R-1"]


# ---------------------------------------------------------------- blocked

def test_blocked_lists_unmet_dependencies():
    # Goals are open items too, so a not-yet-reachable goal is blocked by its needs.
    assert views.blocked(load("diamond.json")) == {
        "demo:B": ["demo:A"], "demo:C": ["demo:A"],
        "demo:GOAL": ["demo:B", "demo:C"]}
    assert views.blocked(load("done_items.json")) == {"demo:GOAL": ["demo:B"]}


def test_blocked_cross_project_and_project_filter():
    cp = load("cross_project.json")
    assert views.blocked(cp) == {"borealis:X": ["atlas:R1"],
                                 "borealis:GOAL": ["borealis:X"]}
    assert views.blocked(cp, project="atlas") == {}


def test_blocked_includes_claimed_items():
    items = [item("demo:X"), item("demo:Y", state="claimed", deps=["demo:X"]),
             item("demo:GOAL", kind="goal", deps=["demo:X"], est=0)]
    assert views.blocked(items) == {"demo:GOAL": ["demo:X"],
                                    "demo:Y": ["demo:X"]}


def test_blocked_ignores_done_dependencies():
    items = [item("demo:A", state="done"), item("demo:B", deps=["demo:A"]),
             item("demo:GOAL", kind="goal", deps=["demo:B"], est=0)]
    assert views.blocked(items) == {"demo:GOAL": ["demo:B"]}


# ---------------------------------------------------------------- waves

def test_waves_diamond_layers():
    assert views.waves(load("diamond.json")) == [
        ["demo:A"], ["demo:B", "demo:C"], ["demo:GOAL"]]


def test_waves_done_dependency_is_satisfied():
    assert views.waves(load("done_items.json")) == [["demo:B"], ["demo:GOAL"]]


def test_waves_mini_snapshot():
    assert views.waves(load("mini_snapshot.json")) == [
        ["atlas:R-1", "borealis:A-10", "borealis:A-3", "borealis:A-4"],
        ["borealis:A-5", "borealis:A-6", "borealis:A-7"],
        ["borealis:A-11"],
        ["borealis:A-9"],
        ["atlas:GOAL"],
    ]


def test_waves_empty():
    assert views.waves(load("empty.json")) == []


# ---------------------------------------------------------------- critical path

def test_critical_path_diamond():
    cp = views.critical_path(load("diamond.json"))
    assert cp == {"keys": ["demo:A", "demo:C", "demo:GOAL"], "hours": 4.0}


def test_critical_path_project_filter():
    cp = views.critical_path(load("mini_snapshot.json"), project="atlas")
    assert cp == {"keys": ["atlas:R-1", "atlas:GOAL"], "hours": 2.0}


def test_critical_path_mini():
    cp = views.critical_path(load("mini_snapshot.json"))
    assert cp["keys"] == ["borealis:A-3", "borealis:A-5", "borealis:A-11",
                          "borealis:A-9", "atlas:GOAL"]
    assert cp["hours"] == 15.0


def test_critical_path_empty():
    assert views.critical_path(load("empty.json")) == {"keys": [], "hours": 0.0}


# ---------------------------------------------------------------- slack

def test_slack_diamond():
    assert views.slack(load("diamond.json")) == {
        "demo:A": 0.0, "demo:B": 1.0, "demo:C": 0.0, "demo:GOAL": 0.0}


def test_slack_mini_zero_on_critical_path():
    items = load("mini_snapshot.json")
    slack = views.slack(items)
    assert set(slack) == {"borealis:A-3", "borealis:A-4", "borealis:A-5", "borealis:A-6",
                          "borealis:A-7", "borealis:A-9", "borealis:A-10", "borealis:A-11",
                          "atlas:R-1", "atlas:GOAL"}
    for key in views.critical_path(items)["keys"]:
        assert slack[key] == 0.0
    assert slack["borealis:A-4"] == 2.0


def test_slack_empty():
    assert views.slack(load("empty.json")) == {}


# ---------------------------------------------------------------- percent_done

def test_percent_done_count_and_estimate():
    items = load("done_items.json")
    assert views.percent_done(items, by="count") == pytest.approx(100 / 3)
    assert views.percent_done(items, by="estimate") == pytest.approx(40.0)


def test_percent_done_excludes_cancelled_and_filters_project():
    items = load("mini_snapshot.json")
    assert views.percent_done(items, by="count") == pytest.approx(100 * 2 / 12)
    assert views.percent_done(items, by="estimate") == pytest.approx(100 * 2 / 30)
    assert views.percent_done(items, by="count", project="atlas") == 0.0


def test_percent_done_empty_is_zero():
    assert views.percent_done(load("empty.json"), by="count") == 0.0


def test_percent_done_rejects_unknown_metric():
    with pytest.raises(ValueError):
        views.percent_done(load("diamond.json"), by="tokens")


# ---------------------------------------------------------------- unblocks

def test_unblocks_counts_non_terminal_dependents():
    ub = views.unblocks(load("diamond.json"))
    assert ub == {"demo:A": 2, "demo:B": 1, "demo:C": 1, "demo:GOAL": 0}


def test_unblocks_ignores_terminal_dependents():
    items = [item("demo:X"), item("demo:Y", state="done", deps=["demo:X"]),
             item("demo:GOAL", kind="goal", deps=["demo:X"], est=0)]
    assert views.unblocks(items)["demo:X"] == 1


def test_unblocks_mini():
    ub = views.unblocks(load("mini_snapshot.json"))
    assert ub["borealis:A-1"] == 1
    assert ub["borealis:A-2"] == 2
    assert ub["atlas:R-1"] == 2
    assert ub["atlas:GOAL"] == 0


# ---------------------------------------------------------------- brief

def test_brief_line_format():
    assert views.brief(load("diamond.json")) == (
        "demo:A | Start work | est 1h | unblocks 2 | prio 0")


def test_brief_respects_n_and_project():
    items = load("mini_snapshot.json")
    assert views.brief(items, n=2) == (
        "atlas:R-1 | Router owns plan definitions | est 2h | unblocks 2 | prio 1\n"
        "borealis:A-10 | Correct snapshot exporter | est 3h | unblocks 1 | prio 1")
    assert views.brief(items, project="atlas") == (
        "atlas:R-1 | Router owns plan definitions | est 2h | unblocks 2 | prio 1")


def test_brief_empty_when_nothing_ready():
    assert views.brief(load("empty.json")) == ""


# ---------------------------------------------------------------- determinism

def test_views_are_order_independent():
    items = load("mini_snapshot.json")
    rev = list(reversed(items))
    assert keys(views.ready(rev)) == keys(views.ready(items))
    assert views.blocked(rev) == views.blocked(items)
    assert views.waves(rev) == views.waves(items)
    assert views.unblocks(rev) == views.unblocks(items)
    assert views.critical_path(rev) == views.critical_path(items)
    assert views.slack(rev) == views.slack(items)
