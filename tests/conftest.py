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
import pytest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


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
        path = tempfile.mkdtemp(prefix="pgtest-", dir=shm)
    except OSError:
        return
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    os.environ["TMPDIR"] = path
    tempfile.tempdir = path


_use_tmpfs_tempdir()

from plangraph.spawner import resolver as _resolver
_real_served_models = getattr(_resolver, '_served_models_real', _resolver.served_models)


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_probe: exercise the real served-model probe, not the host stub')


@pytest.fixture
def real_served_models():
    return _real_served_models


@pytest.fixture(autouse=True)
def hermetic_spawner_host(monkeypatch, tmp_path_factory, request):
    """UNKNOWN host; tests may opt into config with monkeypatch.setenv('AUTOOS_ROOT', ...)."""
    root = tmp_path_factory.mktemp('empty-autoos')
    monkeypatch.setenv('AUTOOS_ROOT', str(root))
    for name in list(os.environ):
        if re.search(r'(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', name, re.I) or name.upper().startswith(('AUTOOS_', 'OMNIROUTE', 'OPENROUTER', 'MIMO', 'DEEPSEEK',
                                    'ANTHROPIC', 'OPENAI', 'GEMINI', 'QODER')) and name != 'AUTOOS_ROOT':
            monkeypatch.delenv(name)
    from plangraph.spawner import resolver
    probe = resolver.served_models
    probe.cache_clear()
    def unknown(config, **kwargs):
        return None
    unknown.cache_clear = probe.cache_clear
    if request.node.get_closest_marker('real_probe') is None:
        monkeypatch.setattr(resolver, 'served_models', unknown)
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
    if not _PG:
        return
    kept, dropped = [], []
    for item in items:
        (dropped if item.nodeid.split("@", 1)[0] in _READS_SQLITE_FILE else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
