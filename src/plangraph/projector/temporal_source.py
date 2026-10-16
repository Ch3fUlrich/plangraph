"""Temporal-backed history source (docs/projector.md, "Source, CLI, reconciler").

``temporalio`` is imported lazily inside the methods, so importing this module never needs the SDK.
The public API is synchronous; every call runs its own short asyncio loop (in a helper thread when
the caller already sits inside a running loop).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Awaitable, Callable, TypeVar

from .source import WorkflowRef

DEFAULT_ADDRESS = "127.0.0.1:7233"
DEFAULT_NAMESPACE = "default"
CONNECT_TIMEOUT_S = 2.0

T = TypeVar("T")


def _run(factory: Callable[[], Awaitable[T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class TemporalSource:
    """``HistorySource`` over a Temporal server.

    ``list_workflows`` returns one ``WorkflowRef`` per *run*: Temporal visibility lists every run of a
    ContinueAsNew chain as its own execution (same workflow id, distinct run id, earlier runs with status
    ``CONTINUED_AS_NEW``); verified against a live dev server (tests/test_projector_temporal_source.py).
    """

    def __init__(self, address: str | None = None, namespace: str | None = None) -> None:
        self.address = address or os.environ.get("TEMPORAL_ADDRESS") or DEFAULT_ADDRESS
        self.namespace = namespace or os.environ.get("TEMPORAL_NAMESPACE") or DEFAULT_NAMESPACE

    async def _connect(self) -> Any:
        from temporalio.client import Client

        return await Client.connect(self.address, namespace=self.namespace)

    def connect_check(self) -> bool:
        """True when the SDK is importable and a server answers within ``CONNECT_TIMEOUT_S`` seconds."""
        try:
            import temporalio  # noqa: F401
        except ImportError:
            return False

        async def check() -> bool:
            await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT_S)
            return True

        try:
            return _run(check)
        except Exception:  # noqa: BLE001 - any failure means "no usable server"
            return False

    def list_workflows(self, *, open_only: bool = False) -> list[WorkflowRef]:
        query = 'ExecutionStatus="Running"' if open_only else ""

        async def go() -> list[WorkflowRef]:
            client = await self._connect()
            refs = [
                WorkflowRef(
                    workflow_id=ex.id,
                    run_id=ex.run_id,
                    start_time=ex.start_time,
                    status=ex.status.name.upper() if ex.status is not None else "",
                )
                async for ex in client.list_workflows(query)
            ]
            refs.sort(key=lambda r: (r.start_time, r.workflow_id, r.run_id))
            return refs

        return _run(go)

    def fetch_history(self, workflow_id: str, run_id: str) -> dict:
        async def go() -> dict:
            client = await self._connect()
            handle = client.get_workflow_handle(workflow_id, run_id=run_id)
            history = await handle.fetch_history()
            return json.loads(history.to_json())

        return _run(go)
