#!/usr/bin/env python3
"""The guard's HANDOFF-NOW / HARD-CAP line names the absolute ready command; the session wrapper never runs `ready`.

A session that follows only the hook text must run the guard script, not the hook wrapper (the wrapper has no
ready command): the line carries `python3 <absolute context_guard.py> ready <handoff path>`.

    python3 -m pytest tests/hooks/test_context_guard_ready_command.py
"""
import json
import os
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, SID, HookCase, load_module, usage_line  # noqa: E402

GUARD = HOOKS / "context_guard.py"
WRAPPER = HOOKS / "ctx_guard_session.py"
HANDOFF = "/work/handoffs/role-0001.md"


def ready_command(line: str) -> str:
    """The backtick-quoted command of a guard line."""
    return line.split("`")[1]


class ReadyCommandInLine(HookCase):
    def test_both_levels_carry_the_absolute_guard_command_and_the_handoff_path(self):
        guard = load_module("context_guard")
        os.environ[guard.HANDOFF_PATH_ENV] = HANDOFF
        self.addCleanup(os.environ.pop, guard.HANDOFF_PATH_ENV, None)
        expected = "python3 %s ready %s" % (shlex.quote(str(GUARD.resolve())), HANDOFF)
        for level, marker in (("handoff", "HANDOFF-NOW"), ("hard", "HARD-CAP")):
            line = guard.emit_line(level, 310000, 300000)
            self.assertTrue(line.startswith(marker), line)
            self.assertEqual(ready_command(line), expected)
            self.assertTrue(os.path.isabs(ready_command(line).split()[1]))
            self.assertNotIn("\n", line)

    def test_without_a_private_destination_the_command_keeps_a_handoff_placeholder(self):
        guard = load_module("context_guard")
        os.environ.pop(guard.HANDOFF_PATH_ENV, None)
        line = guard.emit_line("handoff", 280000, 270000)
        self.assertTrue(ready_command(line).endswith(" ready <handoff-path>"), line)
        self.assertIn(str(GUARD.resolve()), line)

    def test_the_named_command_is_one_the_ready_lock_lets_through(self):
        guard = load_module("context_guard")
        os.environ[guard.HANDOFF_PATH_ENV] = HANDOFF
        self.addCleanup(os.environ.pop, guard.HANDOFF_PATH_ENV, None)
        command = ready_command(guard.emit_line("hard", 310000, 300000))
        self.assertTrue(guard._is_guard_cmd({"tool_name": "Bash", "tool_input": {"command": command}}))

    def test_the_hook_output_through_the_wrapper_keeps_the_command(self):
        self.write(usage_line(read=280000))
        payload = {"transcript_path": str(self.transcript), "session_id": SID, "cwd": "/w/x",
                   "hook_event_name": "PostToolUse"}
        proc = self.run_hook(WRAPPER, payload)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("`python3 %s ready " % shlex.quote(str(GUARD.resolve())), text)


class WrapperHasNoReadyCommand(HookCase):
    def test_wrapper_called_with_ready_exits_2_and_names_the_guard_command(self):
        proc = self.run_hook(WRAPPER, "", args=("ready", HANDOFF))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("python3 %s ready" % shlex.quote(str(GUARD.resolve())), proc.stderr)
        self.assertIn(HANDOFF, proc.stderr)
        self.assertFalse((self.state / "autoos" / "ctx-guard").exists())  # no marker from the wrapper

    def test_wrapper_called_with_ready_and_no_path_still_names_the_guard(self):
        proc = self.run_hook(WRAPPER, "", args=("ready",))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("context_guard.py ready", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
