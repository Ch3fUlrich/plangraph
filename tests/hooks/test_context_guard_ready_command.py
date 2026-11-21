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
            self.assertTrue(os.path.isabs(shlex.split(ready_command(line))[1]))
            self.assertNotIn("\n", line)

    def test_without_a_private_destination_the_command_keeps_a_handoff_placeholder(self):
        guard = load_module("context_guard")
        os.environ.pop(guard.HANDOFF_PATH_ENV, None)
        line = guard.emit_line("handoff", 280000, 270000)
        argv = shlex.split(ready_command(line))
        self.assertEqual(argv, ["python3", str(GUARD.resolve()), "ready", "<handoff-path>"])

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


class SkillNamesTheCommandLikeTheLine(unittest.TestCase):
    def test_skill_says_to_copy_the_emitted_absolute_command_and_hardcodes_no_install_path(self):
        text = (HOOKS.parents[1] / "skills" / "fleet-handoff" / "SKILL.md").read_text(encoding="utf-8")
        step = next(x for x in text.splitlines() if x.startswith("4. "))
        self.assertIn("python3 <absolute-guard-path> ready <handoff-path>", step)
        self.assertIn("copy it from the message", step)
        self.assertIn("ctx_guard_session.py", step)  # the wrapper is named as the wrong script
        for hardcoded in ("tools/hooks/", ".local/share/plangraph/hooks"):
            self.assertNotIn(hardcoded, step)


class WrapperHasNoReadyCommand(HookCase):
    def test_wrapper_called_with_ready_exits_2_and_names_the_guard_command(self):
        proc = self.run_hook(WRAPPER, "", args=("ready", HANDOFF))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("python3 %s ready" % shlex.quote(str(GUARD.resolve())), proc.stderr)
        self.assertIn(HANDOFF, proc.stderr)
        self.assertFalse((self.state / "autoos" / "ctx-guard").exists())  # no marker from the wrapper

    def test_wrapper_called_with_status_exits_2_and_names_the_guard_command(self):
        proc = self.run_hook(WRAPPER, "", args=("status",))
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertIn("python3 %s status" % shlex.quote(str(GUARD.resolve())), proc.stderr)

    def test_a_checkout_path_with_spaces_stays_one_quoted_argument(self):
        import shutil
        spaced = self.tmp / "dir with space" / "hooks"
        spaced.mkdir(parents=True)
        for name in ("context_guard.py", "hook_common.py"):
            shutil.copyfile(HOOKS / name, spaced / name)
        self.write(usage_line(read=280000))
        proc = self.run_hook(spaced / "context_guard.py", {"transcript_path": str(self.transcript), "session_id": SID,
                                                          "cwd": "/w/x"})
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        argv = shlex.split(ready_command(text))
        self.assertEqual(argv[:2], ["python3", os.path.realpath(spaced / "context_guard.py")])

    def test_wrapper_called_with_ready_and_no_path_still_names_the_guard(self):
        proc = self.run_hook(WRAPPER, "", args=("ready",))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("context_guard.py ready", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
