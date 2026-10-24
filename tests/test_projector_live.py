"""Live end-to-end: real workflows on a Temporal dev server this module starts itself.

TemporalSource -> project_source -> Projector -> keeper (SQLite, or Postgres when
``PG_KEEPER_BACKEND=postgres``), then the same histories through FileSource into a second fresh
keeper must give identical rows (rebuild equality).

Skipped when ``temporalio`` is missing or neither ``$TEMPORAL_CLI`` nor ``temporal`` on PATH exists.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("temporalio")

from keeper_util import World, seed  # noqa: E402
from projector_live_util import (  # noqa: E402
    DevServer, LiveRig, ScopedSource, load_recorder, temporal_binary, wait_for)

from plangraph.projector.apply import Projector, project_source  # noqa: E402
from plangraph.projector.source import FileSource  # noqa: E402
from plangraph.projector.temporal_source import TemporalSource  # noqa: E402

_ATTEMPT_COLS = ("key", "n", "harness", "model", "combo", "class", "started_at", "ended_at",
                 "exit_class", "tokens_in", "tokens_out", "cost", "run_id", "workflow_id", "log_ref")
_LEDGER_COLS = ("workflow_id", "run_id", "activity_id", "attempt", "kind", "key", "n")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    binary = temporal_binary()
    if binary is None:
        pytest.skip("no temporal CLI (set TEMPORAL_CLI or put `temporal` on PATH)")
    server = DevServer(binary, tmp_path_factory.mktemp("temporal-dev"))
    server.start()
    rig = LiveRig(server.address, load_recorder())
    try:
        rig.start()
        yield rig, TemporalSource(address=server.address)
    finally:
        try:
            rig.stop()
        finally:
            server.stop()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "live")
    yield w
    w.close()


# ------------------------------------------------------------------- helpers
def projector_for(w: World) -> Projector:
    return Projector(w.store, w.p("system"), now=w.clock)


def attempts(w: World, key: str) -> list[dict]:
    cur = w.conn.execute("SELECT * FROM attempts WHERE key=? ORDER BY n", (key,))
    return [{c: dict(r)[c] for c in _ATTEMPT_COLS} for r in cur]


def ledger(w: World) -> list[tuple]:
    cur = w.conn.execute("SELECT * FROM projector_applied ORDER BY workflow_id, run_id, activity_id,"
                         " attempt, kind")
    return [tuple(dict(r)[c] for c in _LEDGER_COLS) for r in cur]


def item_state(w: World, key: str) -> str:
    return w.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


def event_count(w: World) -> int:
    return w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def snapshot(w: World, keys) -> dict:
    return {"attempts": {k: attempts(w, k) for k in keys}, "ledger": ledger(w),
            "states": {k: item_state(w, k) for k in keys}}


def scheduled_activity_ids(history: dict) -> list[str]:
    return [e["activityTaskScheduledEventAttributes"]["activityId"] for e in history["events"]
            if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"]


def wait_closed(source, wid: str, status: str = "COMPLETED") -> None:
    wait_for(lambda: any(r.workflow_id == wid and r.status == status
                         for r in source.list_workflows()), f"{wid} listed as {status}")


def assert_idempotent(w: World, source, keys) -> None:
    before, events = snapshot(w, keys), event_count(w)
    again = project_source(source, projector_for(w))
    assert again["attempts_written"] == again["states_written"] == 0, again
    assert again["unmapped"] == again["missing_item"] == 0, again
    assert snapshot(w, keys) == before and event_count(w) == events


def assert_rebuild_equal(w: World, source, keys, tmp_path) -> None:
    """The histories as the server holds them, replayed from files into a second fresh keeper."""
    hist_dir = tmp_path / "histories"
    hist_dir.mkdir()
    for i, ref in enumerate(source.list_workflows()):
        doc = {"workflow_id": ref.workflow_id, "run_id": ref.run_id,
               "history": source.fetch_history(ref.workflow_id, ref.run_id)}
        (hist_dir / f"{i:02d}.json").write_text(json.dumps(doc))
    other = World(tmp_path / "rebuilt")
    try:
        seed(other, [(k.split(":", 1)[1], []) for k in keys])
        out = project_source(FileSource(hist_dir), projector_for(other))
        assert out["unmapped"] == out["missing_item"] == 0, out
        assert snapshot(other, keys) == snapshot(w, keys)
        assert_idempotent(other, FileSource(hist_dir), keys)
    finally:
        other.close()


def assert_row(row: dict, *, key, n, exit_class, wid, run_id, activity_id, **extra) -> None:
    assert (row["key"], row["n"], row["exit_class"]) == (key, n, exit_class)
    assert (row["workflow_id"], row["run_id"]) == (wid, run_id)
    assert row["log_ref"] == f"temporal:{activity_id}:1"
    for col, want in extra.items():
        assert row[col] == want, (col, row[col], want)


# ----------------------------------------------------------------- scenarios
def test_ok_attempt(live, world, tmp_path):
    rig, source = live
    key, wid = "demo:LIVE-A", "card:live-a"
    seed(world, [("LIVE-A", [])])
    run_id = rig.start_card("01_ok_single", wid, key)
    rig.result(wid, run_id)
    wait_closed(source, wid)
    scoped = ScopedSource(source, [wid])

    out = project_source(scoped, projector_for(world))
    assert (out["processed"], out["attempts_written"], out["unmapped"]) == (1, 1, 0)
    (act,) = scheduled_activity_ids(source.fetch_history(wid, run_id))
    (row,) = attempts(world, key)
    assert_row(row, key=key, n=1, exit_class="ok", wid=wid, run_id=run_id, activity_id=act,
               harness="demo-harness", model="demo-model", combo="demo-combo", tokens_in=1200,
               tokens_out=300)
    assert item_state(world, key) == "running"        # outcome done: the keeper owns done
    assert [r[4] for r in ledger(world)] == ["attempt", "state"]

    assert_idempotent(world, scoped, [key])
    assert_rebuild_equal(world, scoped, [key], tmp_path)


def test_check_fail_then_ok(live, world, tmp_path):
    rig, source = live
    key, wid = "demo:LIVE-B", "card:live-b"
    seed(world, [("LIVE-B", [])])
    run_id = rig.start_card("02_check_fail_then_ok", wid, key)
    rig.result(wid, run_id)
    wait_closed(source, wid)
    scoped = ScopedSource(source, [wid])

    assert project_source(scoped, projector_for(world))["attempts_written"] == 2
    acts = scheduled_activity_ids(source.fetch_history(wid, run_id))
    first, second = attempts(world, key)
    assert len(acts) == 2 and acts[0] != acts[1]
    assert_row(first, key=key, n=1, exit_class="check_fail", wid=wid, run_id=run_id,
               activity_id=acts[0], tokens_in=1200)
    assert_row(second, key=key, n=2, exit_class="ok", wid=wid, run_id=run_id,
               activity_id=acts[1], tokens_in=2400)
    assert second["log_ref"] == f"temporal:{acts[1]}:1"
    assert item_state(world, key) == "running"

    assert_idempotent(world, scoped, [key])
    assert_rebuild_equal(world, scoped, [key], tmp_path)


def test_running_workflow_projected_mid_flight_then_released(live, world, tmp_path):
    rig, source = live
    key, wid = "demo:LIVE-C", "card:live-c"
    seed(world, [("LIVE-C", [])])
    run_id = rig.start_gate(wid, key)
    scoped = ScopedSource(source, [wid])
    wait_for(lambda: scheduled_activity_ids(source.fetch_history(wid, run_id)),
             "activity scheduled")
    assert [r.status for r in source.list_workflows(open_only=True) if r.workflow_id == wid] == [
        "RUNNING"]
    assert item_state(world, key) == "open"

    mid = project_source(scoped, projector_for(world), open_only=True)
    assert (mid["processed"], mid["attempts_written"], mid["states_written"]) == (1, 0, 1)
    assert attempts(world, key) == [] and item_state(world, key) == "running"
    assert_idempotent(world, scoped, [key])           # second mid-flight pass changes nothing
    assert attempts(world, key) == []

    rig.release(key)
    rig.result(wid, run_id)
    wait_closed(source, wid)
    done = project_source(scoped, projector_for(world))
    assert (done["attempts_written"], done["states_written"]) == (1, 0)
    (act,) = scheduled_activity_ids(source.fetch_history(wid, run_id))
    (row,) = attempts(world, key)
    assert_row(row, key=key, n=1, exit_class="ok", wid=wid, run_id=run_id, activity_id=act,
               harness="gate-harness", model="gate-model")
    assert item_state(world, key) == "running"        # no state change at the end

    third = project_source(scoped, projector_for(world))
    assert third["attempts_written"] == third["states_written"] == 0   # no duplicate attempt
    assert len(attempts(world, key)) == 1
    assert_idempotent(world, scoped, [key])
    assert_rebuild_equal(world, scoped, [key], tmp_path)


def test_heartbeat_timeout_is_lease_lost_and_item_failed(live, world, tmp_path):
    rig, source = live
    key, wid = "demo:LIVE-D", "card:live-d"
    seed(world, [("LIVE-D", [])])
    run_id = rig.start_card("06_heartbeat_timeout", wid, key)
    rig.result(wid, run_id)
    wait_closed(source, wid)
    scoped = ScopedSource(source, [wid])

    out = project_source(scoped, projector_for(world))
    assert (out["attempts_written"], out["states_written"]) == (1, 2)   # running, then failed
    (act,) = scheduled_activity_ids(source.fetch_history(wid, run_id))
    (row,) = attempts(world, key)
    assert_row(row, key=key, n=1, exit_class="lease_lost", wid=wid, run_id=run_id,
               activity_id=act)
    assert item_state(world, key) == "failed"

    assert_idempotent(world, scoped, [key])
    assert_rebuild_equal(world, scoped, [key], tmp_path)
