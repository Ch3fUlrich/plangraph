"""Runtime-owned executors outlive a test without becoming keeper leaks."""
import asyncio
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
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


def _run_scoped_thread_case(tmp_path, scope, leak_test_body, start_in_test,
                           leak_fixture=False):
    """Run the real guard fixtures without the repo's unrelated autouse fixtures."""
    source = Path(__file__).resolve().parent
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "_keeper_threads.py").write_text((source / "_keeper_threads.py").read_text())
    tree = ast.parse((source / "conftest.py").read_text())
    fixtures = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and (node.name == "keeper_thread_guard"
                     or node.name.startswith("no_leaked_keeper_"))]
    fixture_source = ast.unparse(ast.Module(body=fixtures, type_ignores=[]))
    (tests / "conftest.py").write_text("import pytest\n" + fixture_source + '''
import json
from pathlib import Path

reports = []

def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome])

def pytest_sessionfinish(session):
    Path("reports.json").write_text(json.dumps(reports))
''')
    (tests / "test_scope.py").write_text('''
import json
from pathlib import Path
import threading
import pytest
import _keeper_threads as guard

events = []
body_threads = []
elapsed = [0.0]
guard.monotonic = lambda: elapsed[0]

@pytest.fixture(scope=SCOPE)
def keeper_worker():
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="fixture-worker", daemon=True)
    real_join = worker.join
    tearing_down = [False]

    def join(timeout):
        events.append("fixture-join")
        assert tearing_down[0], "fixture worker joined before its scope teardown"
        if LEAK_FIXTURE:
            elapsed[0] += guard.THREAD_SHUTDOWN_TIMEOUT
            return
        release.set()
        real_join(timeout=5)
        assert not worker.is_alive()

    worker.join = join
    worker.start()
    events.append("fixture-start")
    yield worker
    tearing_down[0] = True
    events.append("fixture-teardown")

def test_first(requestFIXTURE_ARGS):
    worker = request.getfixturevalue("keeper_worker")
    assert worker.is_alive()
    events.append("first")
    if LEAK_TEST_BODY:
        release = threading.Event()
        leaked = threading.Thread(target=release.wait, name="body-leak", daemon=True)
        real_join = leaked.join

        def join(timeout):
            elapsed[0] += timeout

        leaked.join = join
        body_threads.append((release, leaked, real_join))
        leaked.start()

def test_second(request):
    worker = request.getfixturevalue("keeper_worker")
    assert worker.is_alive()
    assert events == ["fixture-start", "first"]
    events.append("second")
    for release, leaked, real_join in body_threads:
        release.set()
        real_join(timeout=5)
        assert not leaked.is_alive()

def teardown_module():
    # Module fixture teardown follows this xunit finalizer; record at process exit.
    import atexit
    atexit.register(lambda: Path("events.json").write_text(json.dumps(events)))
'''.replace("SCOPE", repr(scope)).replace("LEAK_TEST_BODY", repr(leak_test_body))
        .replace("LEAK_FIXTURE", repr(leak_fixture))
        .replace("FIXTURE_ARGS", "" if start_in_test else ", keeper_worker"))
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", str(config),
         "--confcutdir", str(tmp_path), str(tests)],
        cwd=tmp_path, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                           "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""},
        capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 1), result.stdout + result.stderr
    reports = json.loads((tmp_path / "reports.json").read_text())
    events = json.loads((tmp_path / "events.json").read_text())
    return result, reports, events


@pytest.mark.parametrize("scope", ["module", "session"])
@pytest.mark.parametrize("start_in_test", [False, True])
def test_guard_joins_fixture_threads_at_their_scope_teardown(tmp_path, scope, start_in_test):
    result, reports, events = _run_scoped_thread_case(tmp_path, scope, False, start_in_test)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(outcome == "passed" for _, _, outcome in reports)
    assert events == ["fixture-start", "first", "second", "fixture-teardown", "fixture-join"]


@pytest.mark.parametrize("scope", ["module", "session"])
@pytest.mark.parametrize("start_in_test", [False, True])
def test_guard_still_fails_test_body_leaks_with_scoped_fixture(tmp_path, scope, start_in_test):
    result, reports, events = _run_scoped_thread_case(tmp_path, scope, True, start_in_test)
    assert result.returncode == 1, result.stdout + result.stderr
    failures = [(node, phase) for node, phase, outcome in reports if outcome == "failed"]
    assert failures == [("tests/test_scope.py::test_first", "teardown")], result.stdout
    assert "Leaked keeper threads: body-leak" in result.stdout
    assert events == ["fixture-start", "first", "second", "fixture-teardown", "fixture-join"]


@pytest.mark.parametrize("scope", ["module", "session"])
def test_guard_fails_fixture_leaks_only_at_scope_teardown(tmp_path, scope):
    result, reports, events = _run_scoped_thread_case(tmp_path, scope, False, True,
                                                    leak_fixture=True)
    assert result.returncode == 1, result.stdout + result.stderr
    failures = [(node, phase) for node, phase, outcome in reports if outcome == "failed"]
    assert failures == [("tests/test_scope.py::test_second", "teardown")], result.stdout
    assert "Leaked keeper threads: fixture-worker" in result.stdout
    assert events == ["fixture-start", "first", "second", "fixture-teardown", "fixture-join"]
