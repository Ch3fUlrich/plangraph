"""Runtime-owned executors outlive a test without becoming keeper leaks."""
import asyncio
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("name,daemon", [("temporal_workflow__0", True), ("asyncio_0", False)])
def test_guard_rejects_keeper_start_even_with_runtime_name(name, daemon, keeper_thread_guard):
    entered, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: (entered.set(), release.wait()),
                              name=name, daemon=daemon)
    join = thread.join
    # A zero-time join makes this deliberately leaked thread deterministic.
    try:
        with patch.object(thread, "join", lambda timeout: join(timeout=0)):
            with pytest.raises(AssertionError, match=f"Leaked keeper threads: {name}"):
                with keeper_thread_guard():
                    thread.start()
                    assert entered.wait(timeout=5)
                    assert thread.is_alive()
    finally:
        release.set()
        join(timeout=5)
    assert not thread.is_alive()


def test_guard_tracks_executor_descendant_by_start_identity(monkeypatch, keeper_thread_guard):
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1)
    threads = []

    def owner():
        threads.append(executor.submit(threading.current_thread).result(timeout=5))

    parent = threading.Thread(target=owner)
    try:
        with monkeypatch.context() as scoped:
            with pytest.raises(AssertionError, match="Leaked keeper threads:"):
                with keeper_thread_guard():
                    parent.start()
                    parent.join(timeout=5)
                    assert not parent.is_alive()
                    child, = threads
                    child.name = "renamed-after-start"
                    join = child.join
                    scoped.setattr(child, "join", lambda timeout: join(timeout=0))
    finally:
        executor.shutdown(wait=True)
        parent.join(timeout=5)
    assert not child.is_alive()


def test_guard_leaves_temporal_workflow_executor_to_worker(keeper_thread_guard):
    pytest.importorskip("temporalio")
    from temporalio.bridge.proto.workflow_activation import (
        RemoveFromCache, WorkflowActivation)
    from temporalio.bridge.worker import PollShutdownError
    from temporalio.common import MetricMeter
    from temporalio.converter import DataConverter
    from temporalio.worker import UnsandboxedWorkflowRunner
    from temporalio.worker._workflow import _WorkflowWorker

    class Bridge:
        async def complete_workflow_activation(self, completion):
            pass

        async def poll_workflow_activation(self):
            raise PollShutdownError()

    options = dict(
        bridge_worker=Bridge, namespace="default", task_queue="guard-test", workflows=[],
        workflow_task_executor=None, max_concurrent_workflow_tasks=1,
        workflow_runner=UnsandboxedWorkflowRunner(),
        unsandboxed_workflow_runner=UnsandboxedWorkflowRunner(),
        data_converter=DataConverter.default, interceptors=[],
        workflow_failure_exception_types=[], patch_activation_callback=None,
        debug_mode=False, disable_eager_activity_execution=False,
        metric_meter=MetricMeter.noop, on_eviction_hook=None, disable_safe_eviction=False,
        should_enforce_versioning_behavior=False, assert_local_activity_valid=lambda _: None,
        encode_headers=False, max_workflow_task_external_storage_concurrency=1)
    # Exercise the real SDK worker without a server, across optional SDK knobs.
    worker = _WorkflowWorker(**{k: v for k, v in options.items()
                               if k in inspect.signature(_WorkflowWorker).parameters})
    threads = []
    worker._running_workflows["guard"] = SimpleNamespace(
        deadlocked_activation_task=None,
        activate=lambda _: threads.append(threading.current_thread()))
    # Temporal creates this pool and shuts it down from _WorkflowWorker.run's
    # finally block. LiveRig.stop already shuts down/awaits its module workers.
    try:
        with keeper_thread_guard():
            asyncio.run(worker._handle_cache_eviction(
                WorkflowActivation(run_id="guard"), RemoveFromCache()))
            thread, = threads
            assert thread.is_alive()
        assert thread.is_alive(), "a per-test guard must not stop a module worker"
    finally:
        asyncio.run(worker.run())
    assert not thread.is_alive()


def test_guard_leaves_playwright_file_executor_to_runtime(tmp_path, keeper_thread_guard):
    pytest.importorskip("playwright")
    from playwright._impl._helper import async_writefile

    loop = asyncio.new_event_loop()
    thread = None
    path = tmp_path / "screenshot.png"
    try:
        with keeper_thread_guard():
            # Page.screenshot(path=...) calls this helper on Playwright's
            # session-owned loop; its default executor survives page.close().
            loop.run_until_complete(async_writefile(path, b"screenshot"))
            thread = loop.run_until_complete(
                loop.run_in_executor(None, threading.current_thread))
            assert thread.is_alive()
        assert thread.is_alive()
        assert path.read_bytes() == b"screenshot"
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    assert not thread.is_alive()
