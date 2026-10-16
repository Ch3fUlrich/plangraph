"""Live tests for ``TemporalSource`` against a running Temporal dev server.

Skipped when ``temporalio`` is missing or no server answers at TEMPORAL_TEST_ADDRESS
(default 127.0.0.1:17233).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("temporalio")
pytest.importorskip("plangraph.projector.source")

from temporalio import activity, workflow  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: E402

from plangraph.projector.temporal_source import TemporalSource  # noqa: E402

ADDRESS = os.environ.get("TEMPORAL_TEST_ADDRESS", "127.0.0.1:17233")
FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
TASK_QUEUE = f"proj-source-{uuid.uuid4().hex[:8]}"


@activity.defn(name="attempt")
async def attempt(args: dict) -> dict:
    return {"exit_class": "ok", "harness": "h", "model": "m", "n": args.get("n", 1)}


@workflow.defn(name="SourceCard")
class SourceCard:
    def __init__(self) -> None:
        self._release = False

    @workflow.signal
    def release(self) -> None:
        self._release = True

    @workflow.run
    async def run(self, args: dict) -> dict:
        if args.get("wait"):
            await workflow.wait_condition(lambda: self._release, timeout=timedelta(seconds=60))
        await workflow.execute_activity("attempt", {"n": 1}, start_to_close_timeout=timedelta(seconds=10))
        if args.get("chain") and args.get("phase", 1) == 1:
            workflow.continue_as_new({**args, "phase": 2})
        return {"outcome": "done"}


@pytest.fixture(scope="module")
def source() -> TemporalSource:
    src = TemporalSource(address=ADDRESS)
    if not src.connect_check():
        pytest.skip(f"no Temporal server at {ADDRESS}")
    return src


def _with_worker(coro_factory):
    async def go():
        client = await Client.connect(ADDRESS)
        async with Worker(client, task_queue=TASK_QUEUE, workflows=[SourceCard], activities=[attempt],
                          workflow_runner=UnsandboxedWorkflowRunner()):
            return await coro_factory(client)

    return asyncio.run(go())


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:10]}"


def _wait_listed(source: TemporalSource, wid: str, *, open_only: bool, want, timeout: float = 20.0) -> list:
    deadline = time.monotonic() + timeout
    while True:
        refs = [r for r in source.list_workflows(open_only=open_only) if r.workflow_id == wid]
        if want(refs):
            return refs
        if time.monotonic() > deadline:
            return refs
        time.sleep(0.3)


def test_import_does_not_need_sdk():
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.modules['temporalio'] = None;"
        "import plangraph.projector.temporal_source as m; m.TemporalSource(); print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code, src_dir], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "other")
    src = TemporalSource()
    assert (src.address, src.namespace) == ("127.0.0.1:1", "other")
    monkeypatch.delenv("TEMPORAL_ADDRESS")
    monkeypatch.delenv("TEMPORAL_NAMESPACE")
    src = TemporalSource()
    assert (src.address, src.namespace) == ("127.0.0.1:7233", "default")


def test_connect_check_false_fast_on_dead_port():
    started = time.monotonic()
    assert TemporalSource(address="127.0.0.1:1").connect_check() is False
    assert time.monotonic() - started < 6


def test_completed_run_listed_and_history_round_trips(source):
    wid = _uid("card")

    async def drive(client):
        return await client.execute_workflow("SourceCard", {}, id=wid, task_queue=TASK_QUEUE)

    assert _with_worker(drive) == {"outcome": "done"}

    refs = _wait_listed(source, wid, open_only=False, want=lambda r: r and r[0].status == "COMPLETED")
    assert len(refs) == 1
    ref = refs[0]
    assert ref.status == "COMPLETED"
    assert ref.run_id
    assert not [r for r in source.list_workflows(open_only=True) if r.workflow_id == wid]

    full = source.list_workflows()
    keys = [(r.start_time, r.workflow_id, r.run_id) for r in full]
    assert keys == sorted(keys)

    hist = source.fetch_history(wid, ref.run_id)
    assert isinstance(hist, dict) and isinstance(hist["events"], list) and hist["events"]
    assert json.loads(json.dumps(hist)) == hist
    assert hist == source.fetch_history(wid, ref.run_id)
    types = [e["eventType"] for e in hist["events"]]
    assert types[0] == "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
    assert types[-1] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"
    assert "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED" in types

    fixture = json.loads((FIXTURES / "01_ok_single.json").read_text())["history"]
    assert set(hist) == set(fixture)
    assert set(hist["events"][0]) == set(fixture["events"][0])


def test_open_only_lists_running(source):
    wid = _uid("card")

    async def drive(client):
        handle = await client.start_workflow("SourceCard", {"wait": True}, id=wid, task_queue=TASK_QUEUE)
        refs = await asyncio.to_thread(
            _wait_listed, source, wid, open_only=True, want=lambda r: bool(r))
        await handle.signal("release")
        await handle.result()
        return refs

    refs = _with_worker(drive)
    assert [r.status for r in refs] == ["RUNNING"]
    done = _wait_listed(source, wid, open_only=False, want=lambda r: r and r[0].status == "COMPLETED")
    assert [r.status for r in done] == ["COMPLETED"]


def test_continue_as_new_chain_lists_each_run(source):
    wid = _uid("card")

    async def drive(client):
        return await client.execute_workflow("SourceCard", {"chain": True}, id=wid, task_queue=TASK_QUEUE)

    _with_worker(drive)
    refs = _wait_listed(
        source, wid, open_only=False,
        want=lambda r: sorted(x.status for x in r) == ["COMPLETED", "CONTINUED_AS_NEW"])
    assert sorted(r.status for r in refs) == ["COMPLETED", "CONTINUED_AS_NEW"]
    assert len({r.run_id for r in refs}) == 2
    first, second = refs  # sorted by start_time
    assert first.status == "CONTINUED_AS_NEW" and second.status == "COMPLETED"
    first_hist = source.fetch_history(wid, first.run_id)
    assert first_hist["events"][-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW"
    assert source.fetch_history(wid, second.run_id)["events"][-1]["eventType"] == (
        "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED")


def test_usable_from_inside_a_running_loop(source):
    async def go():
        return source.list_workflows(open_only=True)

    assert isinstance(asyncio.run(go()), list)


def test_history_accepted_by_projector(source):
    history_mod = pytest.importorskip("plangraph.projector.history")
    wid = _uid("card")

    async def drive(client):
        return await client.execute_workflow("SourceCard", {}, id=wid, task_queue=TASK_QUEUE)

    _with_worker(drive)
    refs = _wait_listed(source, wid, open_only=False, want=lambda r: r and r[0].status == "COMPLETED")
    run_id = refs[0].run_id
    hist = source.fetch_history(wid, run_id)
    proj = history_mod.project_history(hist, workflow_id=wid, run_id=run_id)
    assert proj.workflow_id == wid and proj.run_id == run_id
    assert len(proj.attempts) == 1
