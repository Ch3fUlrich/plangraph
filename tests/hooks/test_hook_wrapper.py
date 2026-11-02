#!/usr/bin/env python3
"""H3: a missing hook script must make the documented wrapper exit 1, and no hook exits 2 for a missing file.

Hooks are registered through the form the installer's usage text documents,

    sh -c 'test -f "$1" || exit 1; exec python3 "$1"' _ <path>

so a vanished or mistyped hook path exits 1 before python3 starts. That is load-bearing: python3 itself exits 2
for a missing script file, exit 2 is the Claude Code block code, and a registered-but-missing PreToolUse guard
would then deny every Bash call. The wrapper under test is parsed out of `install_hooks.sh --help`, so the text
operators copy is the text that is tested.

    python3 tests/hooks/test_hook_wrapper.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, SID, HookCase, clean_env, usage_line  # noqa: E402

SH = shutil.which("sh")
ENTRY_POINTS = ("session_title", "context_guard", "ctx_guard_session", "fleet_inject")


def documented_wrapper():
    """The `sh -c '<body>'` body from the installer's usage text.

    The installer runs in a throwaway HOME with DEST pointed into it: whatever it did with `--help`, nothing
    outside that temp dir could change.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = clean_env(Path(tmp) / "home", DEST=Path(tmp) / "dest")
        usage = subprocess.run([SH, str(HOOKS / "install_hooks.sh"), "--help"], capture_output=True, text=True,
                               env=env, cwd=tmp, timeout=30).stdout
    match = re.search(r"^\s*sh -c '([^']+)' _ ", usage, re.MULTILINE)
    return match.group(1) if match else None


@unittest.skipUnless(SH and os.name != "nt", "the wrapper form needs a POSIX sh")
class WrapperCase(HookCase):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"  # `python3` in the wrapper is the interpreter running the tests
        self.bin.mkdir()
        try:
            (self.bin / "python3").symlink_to(sys.executable)
        except OSError:
            self.skipTest("symlinks are not available")
        self.python = str(self.bin / "python3")
        self.wrapper = documented_wrapper()
        self.assertIsNotNone(self.wrapper, "the installer's usage text must document the wrapper")

    def wrapped(self, path, stdin="", env=None):
        env = dict(env or self.env())
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run([SH, "-c", self.wrapper, "_", str(path)], input=stdin, capture_output=True, text=True,
                              env=env, timeout=30)

    def direct(self, path, stdin="", env=None):
        return subprocess.run([self.python, str(path)], input=stdin, capture_output=True, text=True,
                              env=env or self.env(), timeout=30)


class MissingScript(WrapperCase):
    def assert_gate_closed(self, proc, what):
        self.assertEqual(proc.returncode, 1, "%s must exit 1: %r" % (what, proc.stderr))
        self.assertNotEqual(proc.returncode, 2, "exit 2 is the block code and is reserved")
        self.assertEqual((proc.stdout, proc.stderr), ("", ""), "the gate is silent")

    def test_the_documented_wrapper_is_the_exit_one_form(self):
        self.assertEqual(self.wrapper, 'test -f "$1" || exit 1; exec python3 "$1"')

    def test_h3_a_missing_script_exits_one_never_two(self):
        payload = json.dumps({"session_id": SID})
        for what, path in (("missing file", HOOKS / "no_such_hook.py"),
                           ("file under a missing dir", HOOKS / "no_such_dir" / "hook.py"),
                           ("empty path", ""),
                           ("a directory", HOOKS)):
            self.assert_gate_closed(self.wrapped(path, payload), what)

    def test_h3_every_hook_script_path_that_vanishes_exits_one(self):
        for name in ENTRY_POINTS:
            moved = self.tmp / ("gone-" + name + ".py")
            self.assert_gate_closed(self.wrapped(moved, "{}"), name)

    def test_h3_bare_python_would_have_exited_two(self):
        """The incident shape the wrapper closes."""
        missing = HOOKS / "no_such_hook.py"
        self.assertEqual(self.direct(missing, "{}").returncode, 2, "python3 reports 2 for a missing script")
        self.assertEqual(self.wrapped(missing, "{}").returncode, 1)


class ExistingScript(WrapperCase):
    """An existing script is exec'd untouched: the wrapped run is the direct run."""

    def scenario(self, name, root):
        """(stdin, env) of one fresh run of hook `name` under `root`, with the state it needs."""
        home = root / "home"
        home.mkdir(parents=True)
        env = clean_env(home, XDG_STATE_HOME=root / "state", FLEET_STATE_DIR=root / "fleet",
                        FLEET_SESSION_NAME="fake-parent")
        transcript = root / "t.jsonl"
        transcript.write_text(usage_line(read=301000) + "\n", encoding="utf-8")
        payload = {"session_id": SID, "cwd": str(home), "transcript_path": str(transcript)}
        if name == "fleet_inject":
            inbox = root / "fleet" / "inbox" / "fake-parent.jsonl"
            inbox.parent.mkdir(parents=True)
            inbox.write_text(json.dumps({"ts": 1, "from": "fake-child", "status": "DONE", "line": "DONE x"}) + "\n",
                             encoding="utf-8")
        return json.dumps(payload), env

    def both(self, name, garbled=False):
        runs = []
        for kind in ("direct", "wrapped"):
            root = self.tmp / (name + "-" + kind)
            stdin, env = self.scenario(name, root)
            if garbled:
                stdin = "{not json"
            run = self.direct if kind == "direct" else self.wrapped
            proc = run(HOOKS / (name + ".py"), stdin, env)
            runs.append((proc.returncode, proc.stdout, proc.stderr))
        self.assertEqual(runs[0], runs[1], "wrapped and direct runs must be byte-identical")
        return runs[0]

    def test_a_hook_that_speaks_speaks_through_the_wrapper(self):
        for name, marker in (("session_title", "sessionTitle"), ("context_guard", "HARD-CAP"),
                             ("ctx_guard_session", "HARD-CAP"), ("fleet_inject", "FLEET INBOX")):
            code, out, err = self.both(name)
            self.assertEqual(code, 0, "%s: %s" % (name, err))
            self.assertIn(marker, out, name)

    def test_garbled_stdin_is_the_same_silent_exit_zero(self):
        for name in ENTRY_POINTS:
            self.assertEqual(self.both(name, garbled=True), (0, "", ""), name)


class NeverExitTwo(WrapperCase):
    """The hooks themselves never exit 2 for a missing file (exit 2 blocks the tool call or prompt)."""

    def test_h3_missing_transcript_inbox_and_override_are_exit_zero(self):
        payload = {"session_id": SID, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                   "transcript_path": str(self.tmp / "no" / "t.jsonl")}
        for name in ENTRY_POINTS:
            proc = self.run_hook(HOOKS / (name + ".py"), payload)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (name, proc.stderr))
            if name != "session_title":  # a session without a transcript still gets its counter title
                self.assertEqual(proc.stdout, "", name)

    def test_h3_a_hook_without_its_sibling_modules_never_exits_two(self):
        payload = {"session_id": SID, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                   "transcript_path": str(self.transcript)}
        self.write(usage_line(read=301000))
        for name in ENTRY_POINTS:
            alone = self.tmp / ("alone-" + name)
            alone.mkdir()
            shutil.copy(HOOKS / (name + ".py"), alone)
            proc = self.run_hook(alone / (name + ".py"), payload)
            self.assertIn(proc.returncode, (0, 1), "%s: %s" % (name, proc.stderr))
            self.assertNotEqual(proc.returncode, 2, name)

    def test_h3_fleet_inject_without_fleet_report_says_so_and_exits_zero(self):
        alone = self.tmp / "alone-inject"
        alone.mkdir()
        shutil.copy(HOOKS / "fleet_inject.py", alone)
        proc = self.run_hook(alone / "fleet_inject.py", {"session_id": SID}, FLEET_SESSION_NAME="fake-parent")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("fleet_report.py missing or broken", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
