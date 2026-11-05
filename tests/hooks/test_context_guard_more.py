#!/usr/bin/env python3
"""More tests for tools/hooks/context_guard.py, beside the 36 ported ones (test_context_guard.py).

These pin what the port added over the original (threshold clamp, stepping past unusable records, quoted
commands, the debug traceback) and the `status` output shapes. The AUTOOS_CTX_* names and the `autoos/ctx-guard`
state dir are the live guard's contract; they stay until the cutover item G13.

    python3 tests/hooks/test_context_guard_more.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, SID, HookCase, load_module, usage_line  # noqa: E402

SCRIPT = HOOKS / "context_guard.py"


class MoreCase(HookCase):
    def hook(self, payload=None, **env):
        payload = payload if payload is not None else {
            "transcript_path": str(self.transcript), "session_id": SID, "cwd": "/w/x"}
        return self.run_hook(SCRIPT, payload, **env)

    def text(self, proc):
        self.assertEqual((proc.returncode, proc.stderr), (0, ""), proc.stderr)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def state_dir(self):
        return self.state / "autoos" / "ctx-guard"


class Thresholds(MoreCase):
    def test_a_hard_cap_below_the_handoff_line_is_clamped_up_to_it(self):
        self.write(usage_line(read=120000))
        text = self.text(self.hook(AUTOOS_CTX_HANDOFF="100000", AUTOOS_CTX_HARD="50000"))
        self.assertTrue(text.startswith("HARD-CAP ctx=120k >= 100k"), "never hidden, never a misreported threshold")

    def test_exactly_at_a_clamped_cap_is_hard(self):
        self.write(usage_line(read=100000))
        self.assertTrue(self.text(self.hook(AUTOOS_CTX_HANDOFF="100000", AUTOOS_CTX_HARD="1")).startswith("HARD-CAP"))

    def test_the_env_names_are_the_live_guards(self):
        self.write(usage_line(read=60000))
        self.assertEqual(self.hook().stdout, "")
        self.assertIn("ctx=60k >= 50k", self.text(self.hook(AUTOOS_CTX_HANDOFF="50000")))


class Records(MoreCase):
    def test_a_newer_unusable_record_is_stepped_past_to_an_older_usable_one(self):
        garbled = {"type": "assistant", "message": {"usage": {"input_tokens": "x"}}}
        self.write(usage_line(read=280000), json.dumps(garbled),
                   json.dumps({"type": "assistant", "message": {"content": "no usage"}}))
        self.assertIn("ctx=280k", self.text(self.hook()))

    def test_boolean_usage_fields_are_not_numbers(self):
        self.write(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": True}}}))
        proc = self.hook()
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_a_missing_field_counts_as_zero(self):
        self.write(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 275000}}}))
        self.assertIn("ctx=275k", self.text(self.hook()))

    def test_cwd_must_be_a_string(self):
        self.write(usage_line(read=301000))
        for cwd in (5, None, ["/w"]):
            proc = self.hook({"transcript_path": str(self.transcript), "session_id": SID, "cwd": cwd})
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), repr(cwd))


class Output(MoreCase):
    def test_the_line_names_this_interpreter_and_script(self):
        self.write(usage_line(read=275000))
        command = " ".join('"%s"' % part if " " in part else part for part in (sys.executable, str(SCRIPT.resolve())))
        self.assertIn("then run ready", self.text(self.hook()))

    def test_paths_with_spaces_are_quoted_so_the_command_pastes(self):
        guard = load_module("context_guard")
        line = guard.emit_line("handoff", 280000, 270000, Path("/a b") / "context guard.py")
        resolved = str(Path("/a b/context guard.py").resolve())
        self.assertNotIn(resolved, line)
        self.assertNotIn("\n", line)
        self.assertEqual(guard.emit_line("hard", 310000, 300000, SCRIPT).split(":", 1)[0], "HARD-CAP ctx=310k >= 300k")

    def test_the_debug_variable_surfaces_the_swallowed_traceback(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        self.write(usage_line(read=301000))
        quiet = self.hook(XDG_STATE_HOME=blocker)
        self.assertEqual((quiet.returncode, quiet.stderr), (0, ""))
        loud = self.hook(XDG_STATE_HOME=blocker, AUTOOS_CTX_DEBUG="1")
        self.assertEqual(loud.returncode, 0)
        self.assertIn("Traceback", loud.stderr)

    def test_the_state_file_is_written_after_the_line_and_names_the_session(self):
        self.write(usage_line(read=275000))
        self.text(self.hook())
        state = json.loads((self.state_dir() / ("%s.json" % SID)).read_text(encoding="utf-8"))
        self.assertEqual((state["session_id"], state["ctx"], state["level"], state["cwd"]),
                         (SID, 275000, "handoff", "/w/x"))


class Status(MoreCase):
    def run_cmd(self, *args, **env):
        return self.run_hook(SCRIPT, "", args=args, **env)

    def test_status_shows_a_question_mark_for_a_garbled_fill(self):
        self.state_dir().mkdir(parents=True)
        fills = {"s1": {"session_id": "s1", "level": "hard", "ctx": "lots", "cwd": "/w"},
                 "s2": {"level": "handoff", "ctx": True, "cwd": "/w"}}
        for name, doc in fills.items():
            (self.state_dir() / (name + ".json")).write_text(json.dumps(doc))
        (self.state_dir() / "s3.json").write_text("[1, 2]")
        lines = sorted(self.run_cmd("status").stdout.splitlines())
        self.assertEqual(lines, ["s1 level=hard ctx=?k cwd=/w", "s2 level=handoff ctx=?k cwd=/w"])

    def test_status_lists_ready_markers_with_their_handoff_path(self):
        self.assertEqual(self.run_cmd("ready", "/h/handoff.md", CLAUDE_CODE_SESSION_ID=SID).returncode, 0)
        self.assertEqual(self.run_cmd("status").stdout.strip(), "%s ready handoff=/h/handoff.md" % SID)

    def test_ready_needs_both_a_path_and_a_valid_session_id(self):
        for args, env in (((), {"CLAUDE_CODE_SESSION_ID": SID}), (("/h/x.md",), {}),
                          (("/h/x.md",), {"CLAUDE_CODE_SESSION_ID": "a/b"})):
            proc = self.run_cmd("ready", *args, **env)
            self.assertEqual(proc.returncode, 2, (args, env))
            self.assertIn("context_guard: ready needs", proc.stderr)
        self.assertEqual(list(self.state.rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
