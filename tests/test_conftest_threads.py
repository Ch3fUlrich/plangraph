"""Runtime-owned executors outlive a test without becoming keeper leaks."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import FunctionType

import pytest


@pytest.mark.parametrize("name,daemon", [("temporal_workflow__0", True), ("asyncio_0", False)])
def test_guard_rejects_keeper_start_even_with_runtime_name(name, daemon, keeper_thread_guard):
    entered, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: (entered.set(), release.wait()),
                              name=name, daemon=daemon)
    join = thread.join
    # A zero teardown budget makes this deliberate leak deterministic.
    try:
        with pytest.raises(AssertionError, match=f"Leaked keeper threads: {name}"):
            with keeper_thread_guard(timeout=0):
                thread.start()
                assert entered.wait(timeout=5)
                assert thread.is_alive()
    finally:
        release.set()
        join(timeout=5)
    assert not thread.is_alive()


def test_guard_tracks_executor_descendant_by_start_identity(keeper_thread_guard):
    executor = ThreadPoolExecutor(max_workers=1)
    threads = []

    def owner():
        threads.append(executor.submit(threading.current_thread).result(timeout=5))

    parent = threading.Thread(target=owner)
    try:
        with pytest.raises(AssertionError, match="Leaked keeper threads: renamed-after-start"):
            with keeper_thread_guard(timeout=0):
                parent.start()
                parent.join(timeout=5)
                assert not parent.is_alive()
                child, = threads
                child.name = "renamed-after-start"
    finally:
        executor.shutdown(wait=True)
        parent.join(timeout=5)
    assert not child.is_alive()


def test_guard_leaves_temporal_workflow_executor_to_worker(keeper_thread_guard):
    # Small fake of Temporal SDK 1.36.0, temporalio/worker/_workflow.py:
    # 143-150 creates the pool, 632-637 dispatches activation, 278-280 shuts it
    # down at worker exit. No private SDK constructor/cache/bridge is needed.
    class WorkflowWorker:
        def __init__(self):
            self.executor = ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix="temporal_workflow_")

        async def activate(self):
            return await asyncio.get_running_loop().run_in_executor(
                self.executor, threading.current_thread)

        def shutdown(self):
            self.executor.shutdown(wait=True)

    worker = WorkflowWorker()
    try:
        with keeper_thread_guard():
            thread = asyncio.run(worker.activate())
            assert thread is not threading.current_thread()
            assert thread.name == "temporal_workflow__0"
            assert thread.is_alive()
        assert thread.is_alive(), "a per-test guard must not stop a module worker"
    finally:
        worker.shutdown()
    assert not thread.is_alive()


def test_guard_leaves_playwright_file_executor_to_runtime(tmp_path, keeper_thread_guard):
    # Same observable contract as Playwright 1.64.0, _impl/_helper.py:541-547:
    # async_writefile dispatches a write to the session loop's default pool.
    async def async_writefile(path, data):
        await asyncio.get_running_loop().run_in_executor(None, path.write_bytes, data)

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


def test_guard_leaves_executor_started_from_test_frame(keeper_thread_guard):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with keeper_thread_guard():
            thread = executor.submit(threading.current_thread).result(timeout=5)
            assert thread.is_alive()
        assert thread.is_alive()
    finally:
        executor.shutdown(wait=True)
    assert not thread.is_alive()


def test_guard_rejects_keeper_executor_started_from_test_frame(keeper_thread_guard):
    def submit(executor):
        return executor.submit(threading.current_thread).result(timeout=5)

    # Only the starter's module changes: same executor, target and test caller.
    keeper_submit = FunctionType(submit.__code__,
                                {"__name__": "plangraph.keeper.guard_test",
                                 "threading": threading})
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="keeper-executor")
    try:
        with pytest.raises(AssertionError, match="Leaked keeper threads: keeper-executor_0"):
            with keeper_thread_guard(timeout=0):
                thread = keeper_submit(executor)
                assert thread.is_alive()
        assert thread.is_alive()
    finally:
        executor.shutdown(wait=True)
    assert not thread.is_alive()


def test_guard_waits_past_first_join_for_worker_to_stop(monkeypatch, keeper_thread_guard):
    import _keeper_threads as guard

    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="slow-cleanup", daemon=True)
    real_join = worker.join
    elapsed, joins = [0.0], []

    def join(timeout):
        joins.append(timeout)
        elapsed[0] += timeout
        if elapsed[0] >= 6:
            release.set()
            real_join(timeout=5)

    # Advance a local clock, not wall time: the cleanup stops at logical t=6.
    monkeypatch.setattr(guard, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(worker, "join", join)
    try:
        with keeper_thread_guard():
            worker.start()
            assert worker.is_alive()
        assert elapsed[0] == 6
        assert len(joins) > 1
        assert not worker.is_alive()
    finally:
        release.set()
        real_join(timeout=5)
