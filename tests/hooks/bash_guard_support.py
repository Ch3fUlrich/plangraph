"""Shared helpers for the bash-guard hook tests (tests/hooks/test_bash_guard_*.py).

Not collected by pytest (no ``test_`` prefix). The rules are imported in
process through the ``guard`` fixture, which has ``tools/hooks`` on
``sys.path`` only while importing - a test module never leaves it there. The
hook contract itself (stdin JSON, exit code, stderr) is driven through a real
subprocess with ``run_hook``: a hook that behaves when called but not when run
is not proven.
"""
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "tools" / "hooks"
HOOK = HOOKS / "bash_guard.py"
MIB = 1024 * 1024


@pytest.fixture(scope="module")
def guard():
    """The hook, rules and scanner modules; ``check`` is ``check_command``."""
    sys.path.insert(0, str(HOOKS))
    try:
        import bash_guard
        import bash_guard_rules
        import bash_guard_rules2
    finally:
        sys.path.remove(str(HOOKS))
    return types.SimpleNamespace(hook=bash_guard, rules=bash_guard_rules,
                                 scanner=bash_guard_rules2,
                                 check=bash_guard_rules.check_command)


def cases(table, argnames="name,cmd,needle"):
    """Parametrize a test over table rows, using each row's first item as test id."""
    return pytest.mark.parametrize(argnames, table, ids=[row[0] for row in table])


def payload(command, tool="Bash"):
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


def run_hook(stdin):
    """Run tools/hooks/bash_guard.py with `stdin` (str or bytes); returns the CompletedProcess."""
    data = stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
    return subprocess.run([sys.executable, "-I", str(HOOK)], input=data,
                          capture_output=True, timeout=60)


def run_bash(command):
    return run_hook(payload(command))
