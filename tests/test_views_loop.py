"""MODEL-LOOP: the ``loop`` kind — exit-decision problems and lane-like views.

Item dicts are built exactly like the inline helpers in ``tests/test_views.py``.
"""
import pytest

from plangraph import views


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


def loop_graph(exits, loop_state="open"):
    """A ``demo:LOOP`` plus exit decisions ``(key, state)``, ending at a goal.

    The goal depends on the live exits (or on the loop when there is none), so
    every non-terminal item reaches a goal and nothing else is flagged.
    """
    items = [item("demo:LOOP", kind="loop", state=loop_state)]
    for key, state in exits:
        items.append(item(key, kind="decision", state=state, deps=["demo:LOOP"]))
    live = [key for key, state in exits if state != "cancelled"]
    items.append(item("demo:GOAL", kind="goal", est=0,
                      deps=live or ["demo:LOOP"]))
    return items


# ---------------------------------------------------------------- validate

def test_loop_with_one_decision_exit_has_no_problem():
    assert views.validate(loop_graph([("demo:EXIT", "open")])) == []


def test_loop_without_exit_is_flagged():
    assert codes(views.validate(loop_graph([]))) == ["loop_no_exit"]


def test_loop_with_two_exit_decisions_is_flagged():
    assert codes(views.validate(loop_graph([("demo:E1", "open"),
                                            ("demo:E2", "open")]))) \
        == ["loop_many_exits"]


def test_cancelled_exit_decision_is_not_counted():
    only_dead = loop_graph([("demo:EXIT", "cancelled")])
    assert codes(views.validate(only_dead)) == ["loop_no_exit"]
    dead_and_live = loop_graph([("demo:DEAD", "cancelled"),
                                ("demo:EXIT", "open")])
    assert views.validate(dead_and_live) == []


def test_cancelled_loop_needs_no_exit():
    items = [item("demo:LOOP", kind="loop", state="cancelled"),
             item("demo:GOAL", kind="goal", est=0)]
    assert views.validate(items) == []


def test_only_decision_kinds_count_as_exits():
    # A plain lane depending on the loop is not an exit decision.
    items = [item("demo:LOOP", kind="loop"),
             item("demo:WRONG", kind="lane", deps=["demo:LOOP"]),
             item("demo:GOAL", kind="goal", deps=["demo:WRONG"], est=0)]
    assert codes(views.validate(items)) == ["loop_no_exit"]


# ------------------------------------------- ready / waves behave like a lane

@pytest.mark.parametrize("kind", ["lane", "loop"])
def test_loop_is_ready_blocked_and_waved_like_a_lane(kind):
    items = [item("demo:A", kind=kind),
             item("demo:B", kind=kind, deps=["demo:A"]),
             item("demo:GOAL", kind="goal", deps=["demo:B"], est=0)]
    assert keys(views.ready(items)) == ["demo:A"]
    assert views.blocked(items) == {"demo:B": ["demo:A"],
                                    "demo:GOAL": ["demo:B"]}
    assert views.waves(items) == [["demo:A"], ["demo:B"], ["demo:GOAL"]]


def test_loop_appears_in_waves_next_to_its_exit():
    items = loop_graph([("demo:EXIT", "open")])
    assert views.waves(items) == [["demo:LOOP"], ["demo:EXIT"], ["demo:GOAL"]]
    assert keys(views.ready(items)) == ["demo:LOOP"]
