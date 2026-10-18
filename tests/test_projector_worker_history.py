"""Recorded ``rust/worker`` Card histories project cleanly and apply to the keeper.

The eight fixtures are proto-JSON ``temporal workflow show --output json`` captures of real
``Card`` runs; the projector must read them without coercing an attempt to ``worker_crash`` or
an unknown shape (docs/projector.md, "Contract with the Card workflow (v1)").  Runs on whichever
backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``), same code path.
"""
import json
import re
from pathlib import Path

import pytest

from plangraph.projector.apply import Projector
from plangraph.projector.history import decode_payload, project_history
from plangraph.projector.models import ATTEMPT_ACTIVITY_TYPES, EXIT_CLASSES
from keeper_util import World, seed

FIXTURES = Path(__file__).parents[1] / "rust" / "worker" / "tests" / "fixtures" / "projector"
NAMES = ("happy", "contract_defect", "check_fails", "cancel", "usage_limit_reroute",
         "needs_input_answered", "needs_input_timeout", "gate_blocks")
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
    # RT-P1 inbox: the agent asks, the card waits (item state needs_input), then resumes on the
    # same leg and merges. The question attempt takes no retry budget; the answer arrived as the
    # ``answer`` signal in one fixture and as the question's default after its deadline in the other.
    "needs_input_answered": {
        "status": "completed", "outcome": "done", "exits": ["needs_input", "ok"],
        "states": ["running", "needs_input", "running"],
        "attempt": {"harness": "shell", "model": "some/model", "combo": "leg-1", "class_": "128k"},
    },
    "needs_input_timeout": {
        "status": "completed", "outcome": "done", "exits": ["needs_input", "ok"],
        "states": ["running", "needs_input", "running"],
        "attempt": {"harness": "shell", "model": "some/model", "combo": "leg-1", "class_": "128k"},
    },
    # RT-GATE-WIRING: attempt 1 passes the shell harness, but the pre-merge gate blocks it.
    # The exits are ["ok", "ok"] because the projector reads each attempt's exit_class from
    # the attempt activity result; the pre-merge gate (RT-GATE-WIRING) blocks attempt 1
    # AFTER that activity succeeded, and the correction (check_fail) goes to the keeper
    # through the report activity, not into an attempt result. Known gap, projector view
    # differs from keeper view; fixing it is a projector (HIGH) change out of scope here.
    "gate_blocks": {
        "status": "completed", "outcome": "done", "exits": ["ok", "ok"], "states": ["running"],
        "attempt": {"harness": "shell", "model": "some/model", "combo": "leg-1", "class_": "128k"},
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


def _gate_results(name="gate_blocks"):
    """Decoded results of completed CardActivities::gate activities in fixture ``name``."""
    events = load(name)["events"]
    sched = {ev["eventId"]: ev["activityTaskScheduledEventAttributes"]
             for ev in events if ev["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"}
    results = []
    for ev in events:
        if ev["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
            attrs = ev["activityTaskCompletedEventAttributes"]
            sched_attr = sched.get(attrs.get("scheduledEventId"))
            if sched_attr and sched_attr["activityType"]["name"] == "CardActivities::gate":
                payloads = attrs.get("result", {}).get("payloads", [])
                if payloads:
                    results.append(decode_payload(payloads[0]))
    return results


def test_gate_blocks_schedules_the_gate_before_every_merge():
    names = [a["activityType"]["name"] for a in scheduled("gate_blocks")]
    assert names.count("CardActivities::gate") == 2
    merges = [i for i, n in enumerate(names) if n == "CardActivities::merge"]
    assert len(merges) == 1
    for i in merges:
        assert i > 0 and names[i - 1] == "CardActivities::gate"


def test_gate_blocks_first_gate_failed_second_passed():
    results = _gate_results("gate_blocks")
    assert len(results) == 2
    first, second = results[0], results[1]
    assert first["passed"] is False
    assert second["passed"] is True
    for res in (first, second):
        assert res["checks"]
        assert all(c["name"] == "card-check" for c in res["checks"])
    assert "tail" in first["checks"][0]
    assert first["checks"][0]["tail"] is not None


def test_gate_tail_in_history_is_redacted_shape():
    results = _gate_results("gate_blocks")
    assert results
    hex_40 = re.compile(r"[0-9a-fA-F]{40,}")
    for res in results:
        for check in res.get("checks", []):
            tail = check.get("tail", "")
            assert not hex_40.search(tail)
            assert "API_TOKEN=" not in tail


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


@pytest.mark.parametrize("name", ["needs_input_answered", "needs_input_timeout"])
def test_needs_input_fixtures_raise_no_unknown_shape_alarm(world, projector, name):
    p = project(name, workflow_id=f"card:{KEY}")
    assert p.shape_issues == ()
    projector.apply_run(p, run_ord=0)
    n = world.conn.execute(
        "SELECT COUNT(*) FROM projector_alarms WHERE kind='unknown_shape'").fetchone()[0]
    assert n == 0


@pytest.mark.parametrize("name", ["happy", "check_fails", "needs_input_answered",
                                  "needs_input_timeout", "gate_blocks"])
def test_apply_writes_attempt_rows(world, projector, name):
    # The seeded item is ``demo:A``, so the run applies under the id that yields that plan key.
    p = project(name, workflow_id=f"card:{KEY}")
    assert p.plan_key == KEY and p.attempts
    first = projector.apply_run(p, run_ord=0)
    assert first.attempts_written == len(p.attempts) and first.unmapped == 0
    assert [r["exit_class"] for r in attempts(world)] == [a.exit_class for a in p.attempts]
    again = projector.apply_run(p, run_ord=0)
    assert (again.attempts_written, again.attempts_existing) == (0, len(p.attempts))
    assert len(attempts(world)) == len(p.attempts)
