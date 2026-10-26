"""Record real Temporal histories as projector test fixtures.

Not collected by pytest (no ``test_`` prefix).  Runs a worker with a test ``CardWorkflow`` and an
activity ``attempt`` against a Temporal server, drives the scenarios below, fetches each history and
writes ``<scenario>.json`` = {"workflow_id", "run_id", "history": <proto-JSON dict>} after scrubbing
host-specific values.

    python3 tests/projector_record.py --out tests/fixtures/temporal

Env: TEMPORAL_ADDRESS (default 127.0.0.1:17233), TEMPORAL_NAMESPACE (default "default").
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

TASK_QUEUE = "proj-fixtures"
DEFAULT_ADDRESS = "127.0.0.1:17233"
WAIT_S = 45.0

# (scenario file stem, workflow id, plan_key memo or None)
SCENARIOS: list[tuple[str, str, str | None]] = [
    ("01_ok_single", "card:DEMO-1", "DEMO-1"),
    ("02_check_fail_then_ok", "card:DEMO-2", "DEMO-2"),
    ("03_usage_limit_wait_then_ok", "card:DEMO-3", "DEMO-3"),
    ("04_needs_input_signal", "card:DEMO-4", "DEMO-4"),
    ("05_app_failure", "card:DEMO-5", "DEMO-5"),
    ("06_heartbeat_timeout", "card:DEMO-6", "DEMO-6"),
    ("07_start_to_close_timeout", "card:DEMO-7", "DEMO-7"),
    ("08_cancelled", "card:DEMO-8", "DEMO-8"),
    ("09_continue_as_new", "card:DEMO-9", "DEMO-9"),
    ("10_workflow_failed", "card:DEMO-10", "DEMO-10"),
    ("11_unmapped", "misc-1", None),
    ("12_terminated", "card:DEMO-12", "DEMO-12"),
    ("13_second_attempt_started", "card:DEMO-13", "DEMO-13"),
]


# --------------------------------------------------------------------------- activity

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@activity.defn(name="attempt")
async def attempt(args: dict) -> dict:
    mode = args["mode"]
    n = args["n"]
    plan = args["plan"]
    started = _now()
    if mode == "raise_app":
        raise ApplicationError("contract violated by fixture", type="contract_defect", non_retryable=True)
    if mode == "silent":  # no heartbeat: the server times the activity out
        await asyncio.sleep(4)
    elif mode == "slow":  # outlives START_TO_CLOSE / is cut by terminate
        await asyncio.sleep(3)
    elif mode == "heartbeat_loop":  # heartbeats until the workflow cancels it
        for _ in range(100):
            activity.heartbeat("working")
            await asyncio.sleep(0.2)
    else:
        await asyncio.sleep(0.2)
    if mode == "ok_noclass":
        return {"harness": "demo-harness", "model": "demo-model"}
    exit_class = {"check_fail": "check_fail", "usage_limit": "usage_limit",
                  "needs_input": "needs_input"}.get(mode, "ok")
    return {
        "exit_class": exit_class,
        "harness": "demo-harness",
        "model": "demo-model",
        "combo": "demo-combo",
        "class": "coding",
        "started_at": started,
        "ended_at": _now(),
        "tokens_in": 1200 * n,
        "tokens_out": 300 * n,
        "cost": round(0.0125 * n, 4),
        "log_ref": f"demo://{plan}/attempt-{n}",
    }


# --------------------------------------------------------------------------- workflow

_NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="CardWorkflow")
class CardWorkflow:
    def __init__(self) -> None:
        self._answer: str | None = None

    @workflow.signal
    def answer(self, text: str) -> None:
        self._answer = text

    async def _attempt(self, scenario: dict, n: int, mode: str, **opts: Any) -> dict:
        opts.setdefault("start_to_close_timeout", timedelta(seconds=10))
        return await workflow.execute_activity(
            "attempt", {"mode": mode, "n": n, "plan": scenario["plan"]},
            retry_policy=_NO_RETRY, **opts)

    @workflow.run
    async def run(self, scenario: dict) -> dict:
        name = scenario["scenario"]
        if name in ("01_ok_single", "11_unmapped"):
            await self._attempt(scenario, 1, "ok")
        elif name == "02_check_fail_then_ok":
            await self._attempt(scenario, 1, "check_fail")
            await self._attempt(scenario, 2, "ok")
        elif name == "03_usage_limit_wait_then_ok":
            await self._attempt(scenario, 1, "usage_limit")
            await workflow.sleep(1)
            await self._attempt(scenario, 2, "ok")
        elif name == "04_needs_input_signal":
            await self._attempt(scenario, 1, "needs_input")
            await workflow.wait_condition(lambda: self._answer is not None,
                                          timeout=timedelta(seconds=30))
            await self._attempt(scenario, 2, "ok")
        elif name == "05_app_failure":
            try:
                await self._attempt(scenario, 1, "raise_app")
            except ActivityError:
                return {"outcome": "parked"}
        elif name == "06_heartbeat_timeout":
            try:
                await self._attempt(scenario, 1, "silent", heartbeat_timeout=timedelta(seconds=1))
            except ActivityError:
                return {"outcome": "failed"}
        elif name == "07_start_to_close_timeout":
            try:
                await self._attempt(scenario, 1, "slow", start_to_close_timeout=timedelta(seconds=1))
            except ActivityError:
                return {"outcome": "failed"}
        elif name == "08_cancelled":
            await self._attempt(
                scenario, 1, "heartbeat_loop", start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=2),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
        elif name == "09_continue_as_new":
            if scenario.get("phase", 1) == 1:
                await self._attempt(scenario, 1, "ok")
                workflow.continue_as_new({**scenario, "phase": 2}, memo=workflow.memo() or None)
            await self._attempt(scenario, 2, "ok")
        elif name == "10_workflow_failed":
            await self._attempt(scenario, 1, "ok")
            raise ApplicationError("card failed by fixture", type="card_failed", non_retryable=True)
        elif name == "12_terminated":
            await self._attempt(scenario, 1, "slow", start_to_close_timeout=timedelta(seconds=30))
        elif name == "13_second_attempt_started":
            await self._attempt(scenario, 1, "ok_noclass")
        else:
            raise ApplicationError(f"unknown scenario {name}", non_retryable=True)
        return {"outcome": "done"}


# --------------------------------------------------------------------------- scrubbing

_HOME = re.compile(r"/(?:home|Users|root)/[^\s\"'\\]*")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_AT_HOST = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+")
_ABS_PY = re.compile(r"(?:/[\w.+-]+){2,}\.py")


def _scrub_str(s: str) -> str:
    s = _HOME.sub("<path>", s)
    s = _ABS_PY.sub("<path>", s)
    return _AT_HOST.sub("worker", s)


def scrub(node: Any) -> Any:
    """Replace identities, drop stack traces, neutralise paths/hosts; keep ids and times."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "identity":
                out[k] = "worker"
            elif k == "stackTrace":
                out[k] = ""
            else:
                out[k] = scrub(v)
        return out
    if isinstance(node, list):
        return [scrub(v) for v in node]
    if isinstance(node, str):
        return _scrub_str(node)
    return node


def _all_strings(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_strings(v)
    elif isinstance(node, str):
        yield node
        if len(node) % 4 == 0 and len(node) >= 8:  # maybe a base64 payload
            try:
                yield base64.b64decode(node, validate=True).decode("utf-8")
            except Exception:
                pass


def check_hygiene(doc: dict, label: str) -> None:
    for s in _all_strings(doc):
        if _AT_HOST.search(s):
            raise SystemExit(f"{label}: host-shaped value left in fixture: {s[:80]!r}")
        if _HOME.search(s) or _ABS_PY.search(s):
            raise SystemExit(f"{label}: path left in fixture: {s[:80]!r}")
        for ip in _IPV4.findall(s):
            if ip != "127.0.0.1":
                raise SystemExit(f"{label}: IP left in fixture: {ip}")


# --------------------------------------------------------------------------- recording

async def _history_events(handle: WorkflowHandle, run_id: str | None) -> list[dict]:
    h = handle._client.get_workflow_handle(handle.id, run_id=run_id)
    return json.loads((await h.fetch_history()).to_json()).get("events", [])


async def _wait_event(handle: WorkflowHandle, run_id: str | None, event_type: str,
                      timeout: float = 20.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if any(e.get("eventType") == event_type for e in await _history_events(handle, run_id)):
            return
        await asyncio.sleep(0.2)
    raise SystemExit(f"timeout waiting for {event_type} in {handle.id}")


async def _finish(handle: WorkflowHandle) -> None:
    try:
        await asyncio.wait_for(handle.result(), WAIT_S)
    except WorkflowFailureError:
        pass  # failed / cancelled / terminated: the history is what we record
    except asyncio.TimeoutError:
        raise SystemExit(f"timeout waiting for workflow {handle.id}")


async def _fetch(client: Client, wid: str, rid: str) -> dict:
    hist = await client.get_workflow_handle(wid, run_id=rid).fetch_history()
    return json.loads(hist.to_json())


def _write(out: Path, stem: str, wid: str, rid: str, history: dict) -> list[str]:
    doc = scrub({"workflow_id": wid, "run_id": rid, "history": history})
    check_hygiene(doc, stem)
    (out / f"{stem}.json").write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    return [e["eventType"].removeprefix("EVENT_TYPE_") for e in doc["history"]["events"]]


async def record_all(client: Client, out: Path) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for stem, wid, plan in SCENARIOS:
        scenario = {"scenario": stem, "plan": plan or "MISC-1", "phase": 1}
        try:  # a leftover running workflow from an aborted recording blocks the id
            await client.get_workflow_handle(wid).terminate("recorder cleanup")
        except Exception:
            pass
        handle = await client.start_workflow(
            "CardWorkflow", scenario, id=wid, task_queue=TASK_QUEUE,
            memo={"plan_key": plan} if plan else None,
            execution_timeout=timedelta(minutes=2))
        run1 = handle.result_run_id
        assert run1
        if stem == "04_needs_input_signal":
            await _wait_event(handle, run1, "EVENT_TYPE_ACTIVITY_TASK_COMPLETED")
            await handle.signal("answer", "proceed")
        elif stem == "08_cancelled":
            # the server writes ActivityTaskStarted only when the activity closes, so wait for
            # Scheduled and give the worker time to start it and send a heartbeat
            await _wait_event(handle, run1, "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED")
            await asyncio.sleep(1.0)
            await handle.cancel()
        elif stem == "12_terminated":
            await _wait_event(handle, run1, "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED")
            await asyncio.sleep(1.0)
            await handle.terminate("fixture")
        await _finish(handle)

        if stem == "09_continue_as_new":
            h1 = await _fetch(client, wid, run1)
            can = [e for e in h1["events"]
                   if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW"]
            assert can, "run 1 did not continue as new"
            run2 = can[0]["workflowExecutionContinuedAsNewEventAttributes"]["newExecutionRunId"]
            summary[f"{stem}_run1"] = _write(out, f"{stem}_run1", wid, run1, h1)
            summary[f"{stem}_run2"] = _write(out, f"{stem}_run2", wid, run2,
                                             await _fetch(client, wid, run2))
        else:
            summary[stem] = _write(out, stem, wid, run1, await _fetch(client, wid, run1))
        print(f"recorded {stem}", file=sys.stderr)
    return summary


async def main(out: Path, address: str, namespace: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    client = await Client.connect(address, namespace=namespace)
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[CardWorkflow], activities=[attempt],
                    graceful_shutdown_timeout=timedelta(seconds=5))
    async with worker:
        summary = await record_all(client, out)
    for name, types in summary.items():
        print(f"{name}\t{' '.join(types)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--address", default=os.environ.get("TEMPORAL_ADDRESS", DEFAULT_ADDRESS))
    ap.add_argument("--namespace", default=os.environ.get("TEMPORAL_NAMESPACE", "default"))
    a = ap.parse_args()
    asyncio.run(main(a.out, a.address, a.namespace))
