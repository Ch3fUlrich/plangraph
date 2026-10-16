"""Helpers for the live projector test: its own Temporal dev server and an in-process worker rig.

Not collected by pytest.  The server is a ``temporal server start-dev`` child in its own process
group (killed on stop); the rig runs two workers on a background event loop so the tests stay
synchronous: the recorder's ``CardWorkflow``/``attempt`` pair, and a ``GateCard`` workflow whose
activity blocks on an event the test releases.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from plangraph.projector.temporal_source import TemporalSource

RECORDER_PATH = Path(__file__).with_name("projector_record.py")
RESERVED_PORTS = {17233}             # another dev server runs there; never bind or stop it
GATE_TIMEOUT_S = 30.0


def load_recorder():
    """``tests/projector_record.py`` is not a package: load it from its path."""
    spec = importlib.util.spec_from_file_location("projector_record", RECORDER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["projector_record"] = module
    spec.loader.exec_module(module)
    return module


def temporal_binary() -> str | None:
    cli = os.environ.get("TEMPORAL_CLI")
    if cli and Path(cli).is_file():
        return cli
    return shutil.which("temporal")


def _free_ports(count: int) -> list[int]:
    """``count`` distinct free ports (held open together so they cannot repeat), never reserved ones."""
    socks, ports = [], []
    try:
        while len(ports) < count:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            if port in RESERVED_PORTS:
                s.close()
                continue
            socks.append(s)
            ports.append(port)
    finally:
        for s in socks:
            s.close()
    return ports


class DevServer:
    def __init__(self, binary: str, workdir: Path) -> None:
        self.binary = binary
        self.workdir = Path(workdir)
        self.port, self.ui_port, self.metrics_port = _free_ports(3)
        self.address = f"127.0.0.1:{self.port}"
        self.log = self.workdir / "temporal.log"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 60.0) -> None:
        cmd = [self.binary, "server", "start-dev", "--headless", "--ip", "127.0.0.1",
               "--port", str(self.port), "--ui-port", str(self.ui_port),
               "--metrics-port", str(self.metrics_port),
               "--db-filename", str(self.workdir / "temporal.db")]
        with open(self.log, "wb") as out:
            self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
        source = TemporalSource(address=self.address)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if source.connect_check() and self._namespace_ready(source):
                return
            time.sleep(0.3)
        tail = self.log.read_text(errors="replace")[-1500:]
        self.stop()
        raise RuntimeError(f"temporal dev server did not become ready:\n{tail}")

    @staticmethod
    def _namespace_ready(source: TemporalSource) -> bool:
        """The frontend answers before the ``default`` namespace exists: a listing proves it does."""
        try:
            source.list_workflows()
            return True
        except Exception:  # noqa: BLE001 - not ready yet
            return False

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)           # the dev db is a throwaway: no need for a slow graceful stop
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)


# ------------------------------------------------------------------ gate worker
_GATES: dict[str, asyncio.Event] = {}


def _gate(plan: str) -> asyncio.Event:
    return _GATES.setdefault(plan, asyncio.Event())


@activity.defn(name="attempt")
async def gated_attempt(args: dict) -> dict:
    await asyncio.wait_for(_gate(args["plan"]).wait(), GATE_TIMEOUT_S)
    return {"exit_class": "ok", "harness": "gate-harness", "model": "gate-model",
            "log_ref": f"gate://{args['plan']}"}


@workflow.defn(name="GateCard")
class GateCard:
    @workflow.run
    async def run(self, scenario: dict) -> dict:
        await workflow.execute_activity(
            "attempt", {"plan": scenario["plan"]}, retry_policy=RetryPolicy(maximum_attempts=1),
            start_to_close_timeout=timedelta(seconds=GATE_TIMEOUT_S + 10))
        return {"outcome": "done"}


class LiveRig:
    """Workers plus a client on a background loop; the owning test stays synchronous."""

    def __init__(self, address: str, recorder) -> None:
        self.address = address
        self.recorder = recorder
        self.card_queue = "live-card"
        self.gate_queue = "live-gate"
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.client: Client | None = None
        self.workers: list[Worker] = []
        self.tasks: list[asyncio.Task] = []

    def run(self, coro, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self) -> None:
        self.thread.start()
        self.run(self._boot())

    async def _boot(self) -> None:
        self.client = await Client.connect(self.address)
        common = {"workflow_runner": UnsandboxedWorkflowRunner(),
                  "graceful_shutdown_timeout": timedelta(seconds=1)}
        self.workers = [
            Worker(self.client, task_queue=self.card_queue, workflows=[self.recorder.CardWorkflow],
                   activities=[self.recorder.attempt], **common),
            Worker(self.client, task_queue=self.gate_queue, workflows=[GateCard],
                   activities=[gated_attempt], **common),
        ]
        self.tasks = [asyncio.ensure_future(w.run()) for w in self.workers]

    def stop(self) -> None:
        async def down() -> None:
            for w in self.workers:
                await w.shutdown()
            await asyncio.gather(*self.tasks, return_exceptions=True)

        try:
            self.run(down(), timeout=30)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)

    # -------------------------------------------------------------- workflows
    def start_card(self, scenario: str, wid: str, plan_key: str) -> str:
        """Start the recorder's ``CardWorkflow`` for ``scenario``; returns the run id."""
        return self._start("CardWorkflow", self.card_queue,
                           {"scenario": scenario, "plan": plan_key, "phase": 1}, wid, plan_key)

    def start_gate(self, wid: str, plan_key: str) -> str:
        return self._start("GateCard", self.gate_queue,
                           {"scenario": "gate", "plan": plan_key}, wid, plan_key)

    def _start(self, name: str, queue: str, arg: dict, wid: str, plan_key: str) -> str:
        async def go() -> str:
            handle = await self.client.start_workflow(
                name, arg, id=wid, task_queue=queue, memo={"plan_key": plan_key},
                execution_timeout=timedelta(minutes=2))
            return handle.result_run_id

        return self.run(go())

    def result(self, wid: str, run_id: str, timeout: float = 30.0):
        async def go():
            handle = self.client.get_workflow_handle(wid, run_id=run_id)
            try:
                return await asyncio.wait_for(handle.result(), timeout)
            except WorkflowFailureError as exc:
                return exc

        return self.run(go(), timeout=timeout + 5)

    def release(self, plan_key: str) -> None:
        async def go() -> None:
            _gate(plan_key).set()

        self.run(go())


class ScopedSource:
    """A ``HistorySource`` view over the given workflow ids (the server is shared by every test)."""

    def __init__(self, inner, workflow_ids) -> None:
        self.inner = inner
        self.ids = set(workflow_ids)

    def list_workflows(self, *, open_only: bool = False):
        return [r for r in self.inner.list_workflows(open_only=open_only) if r.workflow_id in self.ids]

    def fetch_history(self, workflow_id: str, run_id: str) -> dict:
        return self.inner.fetch_history(workflow_id, run_id)


def wait_for(predicate, what: str, timeout: float = 20.0, step: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}")
