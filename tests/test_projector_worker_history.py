"""Recorded ``rust/worker`` Card histories project cleanly and apply to the keeper.

The five fixtures are proto-JSON ``temporal workflow show --output json`` captures of real
``Card`` runs; the projector must read them without coercing an attempt to ``worker_crash`` or
an unknown shape (docs/projector.md, "Contract with the Card workflow (v1)").  Runs on whichever
backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``), same code path.
"""
import json
from pathlib import Path

import pytest

from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from plangraph.projector.models import ATTEMPT_ACTIVITY_TYPES, EXIT_CLASSES
from keeper_util import World, seed

FIXTURES = Path(__file__).parents[1] / "rust" / "worker" / "tests" / "fixtures" / "projector"
NAMES = ("happy", "contract_defect", "check_fails", "cancel", "usage_limit_reroute")
KEY = "demo:A"          # the seeded item's key; a run's plan key must equal it to apply

#: Hand-verified projections: status, outcome, attempts' exit classes, states in order, and, for
#: ``happy``, the attempt fields the worker's result carries.
EXPECTED = {
    "happy": {
        "status": "completed", "outcome": "done", "exits": ["ok"], "states": ["running"],
        "attempt": {"harness": "shell", "model": "some/model", "combo": "leg-1", "class_": "128k"},
    },
    "contract_defect": {
        "status": "completed", "outcome": "parked", "exits": ["contract_defect"],
        "states": ["running", "parked"],
    },
    "check_fails": {
        "status": "completed", "outcome": "failed", "exits": ["check_fail", "check_fail"],
        "states": ["running", "failed"],
    },
    "cancel": {
        "status": "completed", "outcome": "parked", "exits": [], "states": ["running", "parked"],
    },
    # D-1533: a Claude-family leg hits its window, the card continues on an opencode leg.
    "usage_limit_reroute": {
        "status": "completed", "outcome": "done", "exits": ["usage_limit", "ok"],
        "states": ["running", "waiting", "running"],
        "attempt": {"harness": "shell", "model": "claude-ish/model", "combo": "claude-leg", "class_": "128k"},
    },
}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def project(name, workflow_id="card:A"):
    """Project fixture ``name``; the plan key comes from the id (the fixtures carry no memo)."""
    return project_history(load(name), workflow_id=workflow_id, run_id="r1")


def scheduled(name):
    """``activityTaskScheduledEventAttributes`` of every scheduled activity, in event order."""
    return [ev["activityTaskScheduledEventAttributes"] for ev in load(name)["events"]
            if ev["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"]


@pytest.mark.parametrize("name", NAMES)
def test_each_fixture_projects(name):
    exp = EXPECTED[name]
    p = project(name)
    assert (p.workflow_id, p.run_id, p.plan_key) == ("card:A", "r1", "A")
    assert (p.status, p.outcome) == (exp["status"], exp["outcome"])
    assert [a.exit_class for a in p.attempts] == exp["exits"]
    assert [s.state for s in p.states] == exp["states"]
    assert p.states[-1].state == exp["states"][-1]
    assert all(a.exit_class in EXIT_CLASSES for a in p.attempts)
    for a in p.attempts:
        assert a.started_at.endswith("Z") and a.ended_at.endswith("Z")
        assert a.started_at <= a.ended_at
    if "attempt" in exp:
        for field, value in exp["attempt"].items():
            assert getattr(p.attempts[0], field) == value


@pytest.mark.parametrize("name", NAMES)
def test_activity_types_are_known(name):
    attrs = scheduled(name)
    names = [a["activityType"]["name"] for a in attrs]
    assert names, f"{name} schedules no activities"
    assert all(n in ATTEMPT_ACTIVITY_TYPES or n.startswith("CardActivities::") for n in names)
    attempt_acts = [a for a in attrs if a["activityType"]["name"] in ATTEMPT_ACTIVITY_TYPES]
    assert attempt_acts, f"{name} schedules no attempt activity"
    assert all(a["retryPolicy"]["maximumAttempts"] == 1 for a in attempt_acts)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])])
    yield w
    w.close()


@pytest.fixture
def projector(world):
    return Projector(world.store, world.p("system"), now=world.clock)


def rows(world, table, order):
    return [dict(r) for r in world.conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def attempts(world, key=KEY):
    return [{k: v for k, v in r.items() if k != "id"}
            for r in rows(world, "attempts", "n") if r["key"] == key]


@pytest.mark.parametrize("name", ["happy", "check_fails"])
def test_apply_writes_attempt_rows(world, projector, name):
    # The seeded item is ``demo:A``, so the run applies under the id that yields that plan key.
    p = project(name, workflow_id=f"card:{KEY}")
    assert p.plan_key == KEY and p.attempts
    assert projector.apply_run(p, run_ord=0).attempts_written == len(p.attempts)
    assert [r["exit_class"] for r in attempts(world)] == [a.exit_class for a in p.attempts]
    again = projector.apply_run(p, run_ord=0)
    assert (again.attempts_written, again.attempts_existing) == (0, len(p.attempts))
    assert len(attempts(world)) == len(p.attempts)
