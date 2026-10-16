"""Rebuild from the 14 recorded histories: determinism, idempotency, known end states.

Runs on whichever backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``), same code path.
"""
import base64
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from plangraph.keeper import events
from plangraph.projector.apply import Projector, project_source
from plangraph.projector.source import FileSource
from keeper_util import World, seed

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
KEYS = [f"demo:DEMO-{i}" for i in range(1, 14)]


def _plan_key_payload(key):
    return {"metadata": {"encoding": base64.b64encode(b"json/plain").decode()},
            "data": base64.b64encode(json.dumps(key).encode()).decode()}


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory):
    """All 14 fixtures, their bare ``DEMO-n`` plan keys rewritten to the seeded ``demo:DEMO-n``."""
    out = tmp_path_factory.mktemp("rebuild_src")
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        started = doc["history"]["events"][0]["workflowExecutionStartedEventAttributes"]
        fields = (started.get("memo") or {}).get("fields") or {}
        if "plan_key" in fields:
            bare = json.loads(base64.b64decode(fields["plan_key"]["data"]))
            fields["plan_key"] = _plan_key_payload(f"demo:{bare}")
        (out / path.name).write_text(json.dumps(doc), encoding="utf-8")
    return out


class HalfSource(FileSource):
    """Lists every run (so run ordinals match); with ``open_only`` only ``part`` counts as open."""

    def __init__(self, directory, part):
        super().__init__(directory)
        self.part = part

    def list_workflows(self, *, open_only=False):
        refs = super().list_workflows()
        half = len(refs) // 2
        chosen = refs[:half] if self.part == 0 else refs[half:]
        return [replace(ref, status="RUNNING" if ref in chosen else "DONE") for ref in refs]


def new_keeper(tmp_path, name):
    world = World(tmp_path / name)
    seed(world, [(f"DEMO-{i}", []) for i in range(1, 14)])
    return world, Projector(world.store, world.p("system"), now=world.clock)


def table(world, sql):
    return [tuple(r) for r in world.conn.execute(sql)]


def snapshot(world):
    return {
        "attempts": [tuple(r) for r in world.conn.execute(
            "SELECT key, n, harness, model, combo, class, started_at, ended_at, exit_class, tokens_in,"
            " tokens_out, cost, run_id, workflow_id, log_ref FROM attempts ORDER BY key, n")],
        "applied": table(world, "SELECT workflow_id, run_id, activity_id, attempt, kind, key, n"
                                " FROM projector_applied ORDER BY workflow_id, run_id, activity_id,"
                                " attempt, kind"),
        "states": table(world, "SELECT key, state FROM items ORDER BY key"),
        "watermark": table(world, "SELECT workflow_id, run_id, run_ord, event_id, status, plan_key"
                                  " FROM projector_watermark ORDER BY workflow_id"),
    }


def counts(world):
    return {t: world.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("attempts", "projector_applied", "projector_watermark", "events", "audit_log")}


@pytest.fixture(scope="module")
def keeper_a(tmp_path_factory, fixture_dir):
    world, projector = new_keeper(tmp_path_factory.mktemp("keeper_a"), "k")
    out = project_source(FileSource(fixture_dir), projector)
    yield world, projector, out
    world.close()


def attempts_of(world, key):
    return [(r[0], r[1]) for r in world.conn.execute(
        "SELECT n, exit_class FROM attempts WHERE key=? ORDER BY n", (key,))]


# ------------------------------------------------------------------- known end states
def test_counters_of_the_full_rebuild(keeper_a):
    _, _, out = keeper_a
    assert out["processed"] == 14 and out["unmapped"] == 1 and out["missing_item"] == 0
    assert out["attempts_written"] == 15


def test_known_end_states(keeper_a):
    world, _, _ = keeper_a
    states = dict(table(world, "SELECT key, state FROM items"))
    assert {k: states[k] for k in KEYS} == {
        "demo:DEMO-1": "running", "demo:DEMO-2": "running", "demo:DEMO-3": "running",
        "demo:DEMO-4": "running", "demo:DEMO-5": "parked", "demo:DEMO-6": "failed",
        "demo:DEMO-7": "failed", "demo:DEMO-8": "parked", "demo:DEMO-9": "running",
        "demo:DEMO-10": "failed", "demo:DEMO-11": states["demo:DEMO-11"],
        "demo:DEMO-12": "parked", "demo:DEMO-13": "running"}
    assert states["demo:DEMO-11"] in ("open", "ready")           # fixture 11 is the unmapped one


def test_known_attempt_histories(keeper_a):
    world, _, _ = keeper_a
    assert attempts_of(world, "demo:DEMO-4") == [(1, "needs_input"), (2, "ok")]
    assert attempts_of(world, "demo:DEMO-2") == [(1, "check_fail"), (2, "ok")]
    assert attempts_of(world, "demo:DEMO-3") == [(1, "usage_limit"), (2, "ok")]
    assert attempts_of(world, "demo:DEMO-5") == [(1, "contract_defect")]
    assert attempts_of(world, "demo:DEMO-6") == [(1, "lease_lost")]
    assert attempts_of(world, "demo:DEMO-7") == [(1, "timeout")]
    assert attempts_of(world, "demo:DEMO-8") == [(1, "cancelled")]
    assert attempts_of(world, "demo:DEMO-9") == [(1, "ok"), (2, "ok")]       # across two runs
    assert attempts_of(world, "demo:DEMO-12") == []
    assert attempts_of(world, "demo:DEMO-13") == [(1, "ok")]
    run_ids = {r[0] for r in world.conn.execute("SELECT run_id FROM attempts WHERE key='demo:DEMO-9'")}
    assert len(run_ids) == 2


def test_the_unmapped_run_wrote_nothing(keeper_a):
    world, _, _ = keeper_a
    assert table(world, "SELECT 1 FROM projector_applied WHERE workflow_id='misc-1'") == []
    assert table(world, "SELECT 1 FROM projector_watermark WHERE workflow_id='misc-1'") == []


def test_continue_as_new_watermark_is_the_newest_run(keeper_a):
    world, _, _ = keeper_a
    row = world.conn.execute("SELECT run_ord, status FROM projector_watermark"
                             " WHERE workflow_id='card:DEMO-9'").fetchone()
    assert (row[0], row[1]) == (1, "completed")


# ---------------------------------------------------------------- re-run is a no-op
def test_rerun_changes_nothing(keeper_a, fixture_dir):
    world, projector, _ = keeper_a
    before, rows = counts(world), snapshot(world)
    head = world.conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    assert events.verify_chain(world.conn) == []
    world.clock.advance(60)
    again = project_source(FileSource(fixture_dir), projector)
    assert again["attempts_written"] == again["states_written"] == 0
    assert again["attempts_existing"] == 15
    assert counts(world) == before
    assert snapshot(world) == rows
    assert world.conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] == head
    assert events.verify_chain(world.conn) == []


# ----------------------------------------------- incremental build == one-shot build
def test_two_halves_equal_the_full_rebuild(keeper_a, fixture_dir, tmp_path):
    world_a, _, _ = keeper_a
    world_b, projector_b = new_keeper(tmp_path, "b")
    try:
        first = project_source(HalfSource(fixture_dir, 0), projector_b, open_only=True)
        second = project_source(HalfSource(fixture_dir, 1), projector_b, open_only=True)
        assert first["processed"] + second["processed"] == 14 and first["processed"] == 7
        assert snapshot(world_b) == snapshot(world_a)
        assert events.verify_chain(world_b.conn) == []
        assert counts(world_b)["events"] == counts(world_a)["events"]
    finally:
        world_b.close()


def test_fresh_keeper_rebuilt_twice_equals_the_first(keeper_a, fixture_dir, tmp_path):
    world_a, _, _ = keeper_a
    world_c, projector_c = new_keeper(tmp_path, "c")
    try:
        project_source(FileSource(fixture_dir), projector_c)
        project_source(FileSource(fixture_dir), projector_c)
        assert snapshot(world_c) == snapshot(world_a)
    finally:
        world_c.close()
