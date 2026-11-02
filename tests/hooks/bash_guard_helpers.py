"""Subprocess runners and the per-row test factory for the ported #292 bash-guard
suites (test_bash_guard_a.py, test_bash_guard_b.py). Not collected by pytest."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "tools" / "hooks" / "bash_guard.py"


def run_hook_raw(stdin_text):
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, timeout=60)


def run_hook_bytes(stdin_bytes):
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_bytes,
                          capture_output=True, timeout=60)


def run_hook(payload):
    return run_hook_raw(json.dumps(payload))


def run_bash(command):
    return run_hook({"tool_name": "Bash", "tool_input": {"command": command}})


def _make_case_test(command, expect_deny, expect_substr):
    def test(self):
        r = run_bash(command)
        info = "command=%r stdout=%r stderr=%r" % (command, r.stdout, r.stderr)
        if expect_deny:
            self.assertEqual(r.returncode, 2, info)
            if expect_substr:
                self.assertIn(expect_substr, r.stderr, info)
        else:
            self.assertEqual(r.returncode, 0, info)
            self.assertEqual(r.stderr, "", info)
    return test
