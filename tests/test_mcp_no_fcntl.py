"""PG-WIN-MCP (D-884): the MCP client imports without ``fcntl`` (Windows).

``plangraph.keeper`` loads its store core lazily, so ``python3 -m
plangraph.keeper.mcp`` pulls in only the stdlib HTTP client.  Each check runs
in a fresh interpreter with ``fcntl`` blocked, except the name-resolution check,
which unblocks it to load the whole surface (POSIX hosts only); no network, no
keeper.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code):
    env = dict(os.environ, PYTHONPATH=SRC)
    env.pop("PG_URL", None)
    env.pop("PG_TOKEN_FILE", None)
    prelude = "import sys; sys.modules['fcntl'] = None\n"
    return subprocess.run([sys.executable, "-I", "-c", prelude + code],
                          env=env, capture_output=True, text=True, timeout=60)


def _run_path(code):
    # -I drops PYTHONPATH, so put src on sys.path by hand.
    return _run(f"sys.path.insert(0, {SRC!r})\n" + code)


def test_mcp_imports_without_fcntl():
    proc = _run_path(
        "import plangraph.keeper.mcp as m\n"
        "assert callable(m.handle)\n"
        "assert 'plangraph.keeper.db' not in sys.modules, 'db loaded'\n"
        "assert 'plangraph.keeper.store' not in sys.modules, 'store loaded'\n"
        "print('ok')\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_mcp_module_entry_starts_without_fcntl():
    # ``python3 -m plangraph.keeper.mcp`` with an empty stdin exits cleanly.
    proc = _run_path(
        "import runpy\n"
        "sys.argv = ['plangraph.keeper.mcp']\n"
        "sys.stdin = __import__('io').StringIO('')\n"
        "try:\n"
        "    runpy.run_module('plangraph.keeper.mcp', run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code in (None, 0), exc.code\n"
        "print('ok')\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


@pytest.mark.skipif(importlib.util.find_spec("fcntl") is None,
                    reason="loading the DB layer needs fcntl (POSIX)")
def test_package_names_still_resolve_lazily():
    proc = _run_path(
        "sys.modules.pop('fcntl')\n"
        "import plangraph.keeper as k\n"
        "assert 'plangraph.keeper.store' not in sys.modules\n"
        "from plangraph.keeper import Store, Principal, Result, Refused\n"
        "from plangraph.keeper import KeeperLocked, open_db, lock_keeper\n"
        "from plangraph.keeper import add_principal, authenticate, revoke\n"
        "from plangraph.keeper.store import Store as S2\n"
        "assert Store is S2\n"
        "assert set(k.__all__) <= set(dir(k))\n"
        "try:\n"
        "    k.no_such_name\n"
        "except AttributeError:\n"
        "    print('ok')\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_db_layer_still_needs_fcntl_explicitly():
    # The lazy surface does not hide the real dependency: the DB layer still
    # refuses to load where fcntl is missing (keeper hosts are POSIX).
    proc = _run_path(
        "try:\n"
        "    from plangraph.keeper import open_db\n"
        "except ImportError:\n"
        "    print('refused')\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "refused"
