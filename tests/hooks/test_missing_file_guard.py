#!/usr/bin/env python3
"""The guarded hook wrapper must map a MISSING hook file to exit 1, never 2.

Every hook here is meant to be installed and driven through the wrapper form

    sh -c 'test -f "$1" || exit 1; exec python3 "$1"' _ <path>

so a vanished or mistyped hook path exits 1 before python3 ever starts. That
distinction is load-bearing: python3 itself exits 2 when the script file is
missing ("can't open file"), and exit 2 is the Claude Code PreToolUse DENY
code - a missing guard must never be mistaken for a guard that denied the
command. The `test -f` gate therefore owns the missing-file outcome (exit 1,
silent) and `exec`s the hook untouched when the file exists, so the wrapped
run is byte-for-byte the direct run.

The wrapper is exercised against the real ported hooks in ``tools/hooks/``:
a blocked Bash command must still come back as exit 2 with the deny message,
an allowed one as silent exit 0 - proving the wrapper runs the hook rather
than swallowing it.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "tools" / "hooks"
BASH_GUARD = HOOKS / "bash_guard.py"
FLEET_REPORT = HOOKS / "fleet_report.py"
WRAPPER = "test -f \"$1\" || exit 1; exec python3 \"$1\""


def run_wrapped(path, stdin_text=""):
    """The wrapper form: `sh -c ...` with the hook path as $1 and payload on stdin."""
    return subprocess.run(["sh", "-c", WRAPPER, "_", str(path)], input=stdin_text,
                          capture_output=True, text=True, timeout=20)


def run_direct(path, stdin_text=""):
    return subprocess.run([sys.executable, str(path)], input=stdin_text,
                          capture_output=True, text=True, timeout=20)


def bash_payload(command):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


BLOCKED = bash_payload("cat <<EOF\n`id`\nEOF")
ALLOWED = bash_payload("ls -la")


class MissingPath(unittest.TestCase):
    """`test -f` owns the missing-file outcome: exit 1, silent, never 2."""

    def assert_gate_closed(self, proc):
        self.assertEqual(proc.returncode, 1, "missing file must exit 1")
        self.assertNotEqual(proc.returncode, 2, "exit 2 is the DENY code and is reserved")
        self.assertEqual((proc.stdout, proc.stderr), ("", ""), "the gate is silent")

    def test_missing_path_exits_one(self):
        self.assert_gate_closed(run_wrapped(HOOKS / "no_such_hook.py", ALLOWED))

    def test_missing_path_under_a_missing_directory_exits_one(self):
        self.assert_gate_closed(run_wrapped(HOOKS / "no_such_dir" / "hook.py", ALLOWED))

    def test_empty_path_exits_one(self):
        self.assert_gate_closed(run_wrapped("", ALLOWED))

    def test_directory_path_exits_one(self):
        self.assert_gate_closed(run_wrapped(HOOKS, ALLOWED))

    def test_missing_file_bare_python_would_have_exited_two(self):
        """The incident shape this gate closes: bare python3 exits 2 on a missing file."""
        proc = run_direct(HOOKS / "no_such_hook.py", ALLOWED)
        self.assertEqual(proc.returncode, 2, "python3 itself reports 2 for a missing script")
        self.assertEqual(run_wrapped(HOOKS / "no_such_hook.py", ALLOWED).returncode, 1)


class ExistingPathRunsTheHook(unittest.TestCase):
    """An existing hook is exec'd untouched: the wrapped run IS the hook's run."""

    def assert_same_as_direct(self, path, stdin_text):
        wrapped = run_wrapped(path, stdin_text)
        direct = run_direct(path, stdin_text)
        self.assertEqual((wrapped.returncode, wrapped.stdout, wrapped.stderr),
                         (direct.returncode, direct.stdout, direct.stderr),
                         "wrapped and direct runs must be byte-identical")

    def test_blocked_command_still_denies(self):
        proc = run_wrapped(BASH_GUARD, BLOCKED)
        self.assertEqual(proc.returncode, 2, "the deny code comes from the hook, not the gate")
        self.assertIn("bash-guard: DENIED", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_allowed_command_is_silent_exit_zero(self):
        self.assert_same_as_direct(BASH_GUARD, ALLOWED)
        proc = run_wrapped(BASH_GUARD, ALLOWED)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_malformed_stdin_still_fails_open(self):
        self.assert_same_as_direct(BASH_GUARD, "{not json")

    def test_fleet_report_hook_runs_through_the_wrapper(self):
        self.assert_same_as_direct(FLEET_REPORT, "{not json")
        proc = run_wrapped(FLEET_REPORT, json.dumps({"session_id": "fake-child"}))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
