"""Shared pieces of tests/test_autoos_refs_*.py.

``ar`` imports tools/autoos_refs_check.py through the ``tools_importable``
fixture (tests/conftest.py), the one way tests reach tools/. Every secret
below is synthetic; ``assert_no_leak`` fails without printing a value, so a
broken redaction never echoes one into a log.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "autoos_refs_check.py"

# Symlink creation is not portable (it needs privileges on Windows), so no
# test creates one without this guard.
windows_skip = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)


@pytest.fixture
def ar(tools_importable):
    """The autoos_refs_check module, with tools/ on sys.path for this test only."""
    import autoos_refs_check
    return autoos_refs_check


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args], capture_output=True, text=True
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def assert_no_leak(secrets, *outputs: str) -> None:
    """Fail, naming no value, when a secret appears in any of *outputs*."""
    for output in outputs:
        for secret in secrets:
            if secret in output:
                pytest.fail("a secret value reached the output (value withheld)",
                            pytrace=False)


def assert_masked(actual: str, expected: str, *secrets: str) -> None:
    """``actual == expected`` once no secret is in *actual*; a leak fails
    without printing it (a plain ``==`` would show both sides)."""
    assert_no_leak(secrets, actual)
    assert actual == expected
