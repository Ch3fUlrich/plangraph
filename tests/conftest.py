"""Make the ``src/`` layout importable without an install step.

With ``PG_KEEPER_BACKEND=postgres`` (the CI ``pytest-postgres`` job) the keeper runs on
Postgres, so tests that open the keeper's SQLite file directly cannot run and are
left out; the contract tests that read the SQLite file are deselected by name (the
frozen suite stays untouched).  Their coverage on Postgres: ``tests/test_keeper_http_events.py``
re-checks the chain over HTTP (head after each write == sha256(previous head + event)) plus
``admin verify``, and ``tests/test_keeper_pg_parity.py`` compares every (seq, hash) pair and the
head against SQLite through the adapter.
"""
import re
import atexit
import os
import shutil
import sys
import tempfile
import threading
import pytest
from pathlib import Path
from pgtest_shm import _sweep_dead_pgtest_dirs

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _oom_worker_count(count, meminfo=Path('/proc/meminfo')):
    override = os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS')
    try:
        count = min(count, max(1, int(override))) if override else count
    except ValueError:
        pass
    try:
        total = int(re.search(r'^MemTotal:\s+(\d+)', meminfo.read_text(), re.M)[1])
    except (OSError, TypeError, ValueError):
        return min(count, 4)  # Unknown capacity is not permission for unbounded workers.
    return max(1, min(count, 4, total // (1024 * 1024)))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    return _oom_worker_count(os.cpu_count() or 1)


@pytest.hookimpl(specname='pytest_configure')
def pytest_oom_configure(config):
    if sys.platform.startswith('linux'):
        from plangraph.spawner.proc_kill import raise_oom_score
        import warnings
        event = raise_oom_score()
        if event['event'] == 'oom_score_adj_denied':
            warnings.warn('oom_score_adj_denied: EACCES raising pytest worker to 500')


@pytest.fixture
def bounded_shutdown_cleanup(monkeypatch):
    """Finish cleanup before a mocked exit can close the database."""
    from plangraph.keeper import daemon
    real_run = daemon.run

    def run(*args, **kwargs):
        record_exit = os._exit  # the test has installed its recorder by now
        preexisting = set(threading.enumerate())

        def exit_and_join(code):
            # Real _exit never returns. Give this run's cleanup a five-second
            # join backstop before the recorder can permit conn.close().
            cleaners = [t for t in threading.enumerate()
                        if t not in preexisting and t.name == "keeper-shutdown"]
            for thread in cleaners:
                thread.join(timeout=5)
                assert not thread.is_alive(), "keeper shutdown did not finish"
            record_exit(code)

        with monkeypatch.context() as patch:
            patch.setattr(os, "_exit", exit_and_join)
            return real_run(*args, **kwargs)

    monkeypatch.setattr(daemon, "run", run)
    yield


@pytest.fixture
def keeper_thread_guard():
    from _keeper_threads import guard_new_threads
    return guard_new_threads


@pytest.fixture(autouse=True)
def no_leaked_keeper_threads(keeper_thread_guard):
    """Bounded-join keeper-owned starts, not session-runtime executor threads."""
    with keeper_thread_guard():
        yield


@pytest.fixture
def w(tmp_path):
    from keeper_util import World
    world = World(tmp_path / "keeper")
    yield world
    world.close()


@pytest.fixture
def tools_importable():
    """Put the repo's ``tools/`` dir on sys.path for one test only.

    tests/test_decisions_render.py used to ``sys.path.insert(0, .../tools)``
    at module import time, leaking the entry into the whole pytest session;
    this fixture scopes the setup and removes the entry again on teardown,
    so importing a tools/ module never mutates the session-wide sys.path.
    """
    tools = str(_TOOLS)
    added = tools not in sys.path
    if added:
        sys.path.insert(0, tools)
    try:
        yield tools
    finally:
        if added and tools in sys.path:
            sys.path.remove(tools)

_NEUTRAL_POLICY = '{"banned_families": [], "off_families": [], "paused_harnesses": {}}'
# Module-level route plans (computed at collection) see the same neutral route
# policy as the per-test fixture below.
_policy_dir = tempfile.mkdtemp(prefix="pg-route-policy-")
atexit.register(shutil.rmtree, _policy_dir, True)
Path(_policy_dir, "route-policy.json").write_text(_NEUTRAL_POLICY)
os.environ["PLANGRAPH_ROUTE_POLICY"] = str(Path(_policy_dir, "route-policy.json"))


def _use_tmpfs_tempdir() -> None:
    # The suite is fsync-bound (SQLite commits); tmpfs avoids slow-disk stalls.
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        # The controller's TMPDIR is inherited. Give non-pytest daemon helpers
        # their own worker root too (tmp_path is already isolated by xdist).
        parent = os.environ.get("TMPDIR")
        path = tempfile.mkdtemp(prefix=f"pgtest-{worker}-", dir=parent)
        atexit.register(shutil.rmtree, path, ignore_errors=True)
        os.environ["TMPDIR"] = path
        tempfile.tempdir = path
        return
    shm = "/dev/shm"
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("TMPDIR") or os.environ.get("PG_TEST_DISK_TMP"):
        return
    try:
        if not os.access(shm, os.W_OK | os.X_OK):
            return
        st = os.statvfs(shm)
        if st.f_bavail * st.f_frsize < 256 * 1024 * 1024:
            return
        _sweep_dead_pgtest_dirs(shm)
        path = tempfile.mkdtemp(prefix=f"pg{os.getpid()}-", dir=shm)
    except OSError:
        return
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    os.environ["TMPDIR"] = path
    tempfile.tempdir = path


_use_tmpfs_tempdir()


@pytest.fixture(autouse=True)
def hermetic_writer_env(monkeypatch):
    """Writer identity/config must not override a test's own setup."""
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def short_socket_dir():
    """Bound Unix sockets avoid pytest's long per-test directory names."""
    with tempfile.TemporaryDirectory(prefix="s-", dir=tempfile.gettempdir()) as directory:
        path = Path(directory)
        assert len(os.fsencode(path / "anchor.sock")) < 108
        assert len(os.fsencode(path / "silent.sock")) < 108
        yield path

from plangraph.spawner import resolver as _resolver
_real_served_models = getattr(_resolver, '_served_models_real', _resolver.served_models)


def _remove_windows_basetemp(root):
    """Retry transient child-process handles as well as read-only git objects."""
    import time
    from _pytest.pathlib import rm_rf
    # Failed assertions in mkstemp hooks can strand CRT descriptors before the
    # caller receives them. At process exit, close only files in our owned root.
    identities = set()
    for path in Path(root).rglob("*"):
        if path.is_file():
            st = path.stat()
            identities.add((st.st_dev, st.st_ino))
    for fd in range(3, 2048):
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) in identities:
                os.close(fd)
        except OSError:
            pass
    for attempt in range(31):
        try:
            rm_rf(Path(root))
            return
        except PermissionError:
            if attempt == 30:
                raise  # report the leftover rather than silently leaking it
            time.sleep(1)


def pytest_configure(config):
    if sys.platform == "win32" and config.option.basetemp is None:
        # Avoid numbered-root/pytest-current symlink cleanup on Windows mounts
        # affected by the reported WinError 448 (not reproduced on this run).
        # A fresh owned root avoids that machinery without touching old roots.
        root = tempfile.mkdtemp(prefix="pg-win-pytest-")
        config.option.basetemp = str(Path(root) / "tests")
        atexit.register(_remove_windows_basetemp, root)
    config.addinivalue_line('markers', 'real_probe: exercise the real served-model probe, not the host stub')
    config.addinivalue_line('markers', 'writer_change_check: keep the D-3236 writer no-change check live')


@pytest.fixture
def real_served_models():
    return _real_served_models


@pytest.fixture(autouse=True)
def hermetic_spawner_host(monkeypatch, tmp_path_factory, request):
    """UNKNOWN host; tests may opt into config with monkeypatch.setenv('AUTOOS_ROOT', ...)."""
    root = tmp_path_factory.mktemp('empty-autoos')
    monkeypatch.setenv('AUTOOS_ROOT', str(root))
    state = tmp_path_factory.mktemp('xdg-state')  # never the production spawner/leg state (D-2844)
    monkeypatch.setenv('XDG_STATE_HOME', str(state))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(state / 'plangraph'))
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '0')
    # Services remain default-on, but never construct the production adapter.
    from plangraph.routing_support import leg_state_flag
    from types import SimpleNamespace
    reader = SimpleNamespace(enabled=True, leg_status=lambda *a, **k: dict(state='ok', evidence=None))
    monkeypatch.setattr(leg_state_flag, 'default_reader', lambda: reader)
    # Route mechanics tests run under a neutral route policy; the operator's
    # live lists (planner/route_policy.json) are pinned in test_registry_route.py.
    neutral = state / 'route-policy.json'
    neutral.write_text(_NEUTRAL_POLICY)
    monkeypatch.setenv('PLANGRAPH_ROUTE_POLICY', str(neutral))
    # Credential scrubbing below also removes GIT_CONFIG_KEY_*; do not leave
    # an inherited count pointing at missing key/value pairs in subprocesses.
    for name in list(os.environ):
        if name == 'GIT_CONFIG_COUNT' or name.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
            monkeypatch.delenv(name)
    for name in list(os.environ):
        if re.search(r'(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', name, re.I) or name.upper().startswith(('AUTOOS_', 'OMNIROUTE', 'OPENROUTER', 'MIMO', 'DEEPSEEK',
                                    'ANTHROPIC', 'OPENAI', 'GEMINI', 'QODER')) and name != 'AUTOOS_ROOT':
            monkeypatch.delenv(name)
    # An inherited OPENCODE_CONFIG_CONTENT is a gateway endpoint source (its
    # providers.mimo/omniroute settings.baseURL defeats the I6 fail-closed
    # path); tests opt into config with monkeypatch.setenv, never inherit it.
    monkeypatch.delenv('OPENCODE_CONFIG_CONTENT', raising=False)
    # Admission renders gateway providers like launch (I6): a dead-port URL keeps
    # gateway legs admissible; no-URL tests delenv it themselves.
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://127.0.0.1:9/v1')
    # Fake writers exit 0 without touching the checkout they run in; the D-3236
    # no-change check is exercised only by tests marked writer_change_check.
    if 'writer_change_check' not in request.keywords:
        from plangraph.spawner import opencode_isolation
        monkeypatch.setattr(opencode_isolation, 'record_baseline', lambda job, work_dir, live_jobs=(): None)
    from plangraph.spawner import resolver
    probe = resolver.served_models
    probe.cache_clear()
    def unknown(config, **kwargs):
        return None
    unknown.cache_clear = probe.cache_clear
    if request.node.get_closest_marker('real_probe') is None:
        monkeypatch.setattr(resolver, 'served_models', unknown)
        # Async spawn retains the real-function alias; stub both entry points.
        monkeypatch.setattr(resolver, '_served_models_real', unknown)
        def forbidden_probe(*args, **kwargs):
            pytest.fail('Host binary probe requires @pytest.mark.real_probe', pytrace=False)
        monkeypatch.setattr(resolver, '_probe_served_models', forbidden_probe)
    original_which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda command, *a, **kw:
                        (original_which(command, *a, **kw) or command)
                        if command in ('agy', 'qodercli') else original_which(command, *a, **kw))
    yield
    probe.cache_clear()

_PG = os.environ.get("PG_KEEPER_BACKEND") == "postgres"

#: tests that read the SQLite file itself (contract: dump/copy ``keeper.db`` via stdlib sqlite3).
_READS_SQLITE_FILE = {
    "tests/test_keeper_brief.py::test_brief_stats_reports_median_and_p90",   # opens the file mode=ro
    "tests/test_keeper_cli.py::test_cli_admin_init_add_principal_verify",    # inspects the db file
    "tests/contract/test_events.py::test_chain_verifies_from_outside",
    "tests/contract/test_events.py::test_event_payload_and_actor_attribution",
    "tests/contract/test_events.py::test_admin_verify_passes_and_flags_tampering",
}


def _targets_browser_dir(config) -> bool:
    """True when the invocation explicitly points at tests/browser (path or -m)."""
    args = list(getattr(config.option, "file_or_dir", None) or [])
    if any("browser" in str(a) for a in args):
        return True
    return "browser" in (getattr(config.option, "markexpr", "") or "")


def pytest_ignore_collect(collection_path, config):
    # UI-BROWSER-TESTS W1: tests/browser needs Playwright and runs in its own CI
    # job, so the default ``pytest tests`` never collects it (explicit paths win).
    if collection_path.name == "browser" and collection_path.parent.name == "tests":
        if not _targets_browser_dir(config):
            return True
    if _PG and collection_path.suffix == ".py" and collection_path.name.startswith("test_"):
        if "contract" not in collection_path.parts:
            try:
                text = collection_path.read_text(encoding="utf-8")
            except OSError:
                return None
            if "sqlite3" in text and "test_keeper_pg_" not in collection_path.name:
                return True
    return None


def pytest_collection_modifyitems(config, items):
    if sys.platform == "win32":
        from win_skips import skip_reason
        for item in items:
            reason = skip_reason(item.nodeid, sys.platform, item.fixturenames)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))
    if not _PG:
        return
    kept, dropped = [], []
    for item in items:
        (dropped if item.nodeid.split("@", 1)[0] in _READS_SQLITE_FILE else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


@pytest.fixture
def fake_ownership(monkeypatch):
    """r3 V3: stub pids are "ours" (fixed token) and invisible in /proc, so no test depends on which
    host pids are live; our real descendants keep their real identity.  Opt-in per file."""
    import os
    from plangraph.spawner import proc_kill
    from plangraph.spawner.runner import SpawnerRunner
    real_stat, real_identity = proc_kill._stat, proc_kill.root_identity

    born = real_stat(os.getpid())[4]   # only processes started after this test session exist for the stubbed view
    monkeypatch.setattr(proc_kill, '_stat', lambda pid, root='/proc': (lambda st: st if st and st[4] >= born else None)(real_stat(pid, root)))
    monkeypatch.setattr(SpawnerRunner, 'identity_provider', staticmethod(lambda pid, **k: real_identity(pid, **k) or 'fake-token'))

import _merge_gate_support


@pytest.fixture
def core(tools_importable):
    import merge_gate_core
    return merge_gate_core


@pytest.fixture
def merge_gate(core):
    import merge_gate
    return merge_gate


@pytest.fixture
def facts():
    return _merge_gate_support.facts()


@pytest.fixture
def refuse(core):
    return lambda facts, code: _merge_gate_support.refuse(core, facts, code)


@pytest.fixture(autouse=True)
def no_effects(request, monkeypatch):
    if request.node.path.name in ("test_merge_gate.py", "test_merge_gate_fail_closed.py"):
        _merge_gate_support.no_effects(monkeypatch, request.getfixturevalue("core"),
                                       request.getfixturevalue("merge_gate"))
