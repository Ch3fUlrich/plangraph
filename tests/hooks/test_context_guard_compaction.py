"""Carried AutoOS compaction contracts and deliberate plangraph differences."""
import json
import os
import shutil
import sys
from unittest.mock import patch

from hookkit import HOOKS, SID, load_module, usage_line
from test_context_guard import GuardCase

BOUNDARY = json.dumps({"type": "system", "subtype": "compact_boundary"})


class Compaction(GuardCase):
    def fleet(self, *lines):
        self.write(json.dumps({"type": "agent-name", "agentName": "builder-0042"}), *lines)

    def test_compaction_below_threshold_without_environment(self):
        self.fleet(BOUNDARY, usage_line(inp=1000))
        text = self.ctx_text(self.hook(FLEET_SESSION_NAME=""))
        self.assertIn("HANDOFF-NOW ctx=1k (compaction; handoff required)", text)
        self.assertIn("then run ready", text)
        state = json.loads(self.state_file().read_text())
        self.assertEqual((state["level"], state["compaction"] > 0), ("handoff", True))

    def test_interactive_compaction_also_requires_handoff(self):
        self.write(json.dumps({"type": "custom-title", "customTitle": "interactive"}),
                   BOUNDARY, usage_line(inp=1000))
        self.assertIn("HANDOFF-NOW", self.ctx_text(self.hook(FLEET_SESSION_NAME="")))

    def test_boundary_without_identity_or_usage_requires_handoff(self):
        self.write(BOUNDARY)
        self.assertIn("HANDOFF-NOW", self.ctx_text(self.hook(FLEET_SESSION_NAME="")))

    def test_compaction_above_hard_cap_still_says_handoff_now(self):
        self.write(BOUNDARY, usage_line(inp=301000))
        text = self.ctx_text(self.hook(FLEET_SESSION_NAME=""))
        self.assertIn("HANDOFF-NOW", text)
        self.assertIn("HARD-CAP", text)

    def test_low_context_without_boundary_clears_state(self):
        self.fleet(usage_line(inp=280000))
        self.hook()
        with self.transcript.open("a") as fh:
            fh.write(usage_line(inp=1000) + "\n")
        self.assertEqual(self.hook().stdout, "")
        self.assertFalse(self.state_file().exists())

    def test_pending_survives_and_reemits_by_usual_rule(self):
        self.fleet(BOUNDARY, usage_line(inp=2000))
        self.assertIn("HANDOFF-NOW", self.ctx_text(self.hook()))
        self.assertEqual(self.hook().stdout, "")
        self.assertTrue(self.state_file().exists())
        with self.transcript.open("a") as fh:
            fh.write(usage_line(inp=1000) + "\n")
        self.assertIn("compaction", self.ctx_text(self.hook()))
        with self.transcript.open("a") as fh:
            fh.write(BOUNDARY + "\n")
        self.assertIn("compaction", self.ctx_text(self.hook()))

    def test_long_transcript_cursor_does_not_rescan_prefix(self):
        self.fleet(*(['{}'] * 150000), usage_line(inp=1000))
        self.hook()
        size = self.transcript.stat().st_size
        with self.transcript.open("r+b") as fh:
            fh.seek(100)
            fh.write(("\n" + BOUNDARY + "\n").encode())
        with self.transcript.open("a") as fh:
            fh.write(usage_line(inp=1000) + "\n")
        self.assertEqual(self.hook().stdout, "")
        path = self.state_file().parent.with_name("ctx-guard-scan") / (SID + ".scan")
        self.assertGreaterEqual(json.loads(path.read_text())["offset"], size)

    def test_scan_partial_lines_identity_and_truncation(self):
        scan = load_module("context_compaction").scan
        self.write(json.dumps({"type": "custom-title", "customTitle": "builder-0042"}))
        with self.transcript.open("ab") as fh:
            fh.write(BOUNDARY.encode())
        first = scan(self.transcript, None)
        self.assertTrue(first["fleet"])
        self.assertNotIn("boundary", first)
        with self.transcript.open("ab") as fh:
            fh.write(b"\n")
        second = scan(self.transcript, first)
        self.assertEqual(second["boundary"], self.transcript.stat().st_size)
        self.write("{}")
        self.assertNotIn("fleet", scan(self.transcript, second))
        other = self.tmp / "other.jsonl"
        # Span the old cursor so only an identity reset finds this boundary.
        other.write_bytes((BOUNDARY + " " * second["offset"] + "\n").encode())
        self.assertGreater(other.stat().st_size, second["offset"])
        self.assertEqual(scan(other, second)["boundary"], other.stat().st_size)

    def test_scan_oversized_record_between_markers(self):
        mod = load_module("context_compaction")
        self.fleet("x" * (mod.LINE_CAP + 10), BOUNDARY)
        with self.transcript.open("rb") as fh, patch.object(mod.Path, "open", return_value=fh), \
                patch.object(fh, "readline", wraps=fh.readline) as read:
            state = mod.scan(self.transcript, None)
            self.assertTrue(all(args == (mod.LINE_CAP,) for args, _ in read.call_args_list))
        self.assertTrue(state["fleet"])
        self.assertEqual(state["boundary"], self.transcript.stat().st_size)
        self.assertEqual(state["offset"], self.transcript.stat().st_size)

    def test_scan_oversized_partial_record_waits_for_newline(self):
        mod = load_module("context_compaction")
        self.write("{}")
        start = self.transcript.stat().st_size
        with self.transcript.open("ab") as fh:
            fh.write(b"x" * (mod.LINE_CAP + 10))
        first = mod.scan(self.transcript, None)
        self.assertEqual(first["offset"], start)
        with self.transcript.open("ab") as fh:
            fh.write(b"\n")
        self.assertEqual(mod.scan(self.transcript, first)["offset"], self.transcript.stat().st_size)

    def test_scan_non_dict_cursor_starts_fresh(self):
        scan = load_module("context_compaction").scan
        self.fleet(BOUNDARY)
        for previous in (5, [1, 2]):
            with self.subTest(previous=previous):
                self.assertEqual(scan(self.transcript, previous), scan(self.transcript, None))

    def test_public_helpers_and_state_fields(self):
        guard = load_module("context_guard")
        self.assertEqual(guard.fill_from_usage({"input_tokens": 2, "cache_read_input_tokens": 3,
                                               "cache_creation_input_tokens": 4, "output_tokens": 99}), 9)
        for usage in (None, [], {"input_tokens": True}, {"input_tokens": "2"}):
            self.assertIsNone(guard.fill_from_usage(usage))
        self.assertEqual(guard.fill_from_usage({}), 0)
        self.assertEqual(guard.resolve_thresholds(10, 5), (10, 5))
        with patch.dict(os.environ, self.env(AUTOOS_CTX_HANDOFF="12", AUTOOS_CTX_HARD="20"), clear=True):
            self.assertEqual(guard.resolve_thresholds(), (12, 20))
            self.assertEqual(guard.resolve_thresholds(0, 5), (0, 5))
        for ctx, level, threshold in ((270000, "handoff", 270000), (300000, "hard", 300000)):
            self.write(usage_line(read=ctx))
            self.assertEqual(self.ctx_text(self.hook()), guard.emit_line(level, ctx, threshold))
            state = json.loads(self.state_file().read_text())
            self.assertEqual(state, dict(session_id=SID, cwd="/w/x", ctx=ctx, ts=state["ts"],
                                         level=level, compaction=None))
        self.write(usage_line(inp=1))
        self.assertEqual(self.hook().stdout, "")
        self.assertFalse(self.state_file().exists())

    def test_missing_compaction_module_keeps_hard_cap_guidance(self):
        import io
        self.write(usage_line(inp=301000))
        payload = json.dumps({"transcript_path": str(self.transcript), "session_id": SID, "cwd": "/w/x"})
        with patch.dict(sys.modules, {"context_compaction": None}), \
                patch.dict(os.environ, self.env(), clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            guard = load_module("context_guard")
            with patch.object(guard, "read_stdin_text", return_value=payload):
                self.assertEqual(guard.run_hook(), 0)
            self.assertIn("HARD-CAP ctx=301k >= 300k", out.getvalue())
            text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(text, guard.emit_line("hard", 301000, 300000))
        scan_path = self.state_file().parent.with_name("ctx-guard-scan") / (SID + ".scan")
        self.assertFalse(scan_path.exists())

    def broken_compaction_hook(self, body, **env):
        for name in ("context_guard.py", "hook_common.py"):
            shutil.copyfile(HOOKS / name, self.tmp / name)
        (self.tmp / "context_compaction.py").write_text(body, encoding="utf-8")
        self.write(usage_line(inp=301000))
        payload = {"transcript_path": str(self.transcript), "session_id": SID, "cwd": "/w/x"}
        proc = self.run_hook(self.tmp / "context_guard.py", payload, PYTHONDONTWRITEBYTECODE="1", **env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("HARD-CAP ctx=301k >= 300k", proc.stdout)
        with patch.dict(os.environ, self.env(), clear=True):
            self.assertEqual(self.ctx_text(proc), load_module("context_guard").emit_line("hard", 301000, 300000))
        scan_path = self.state_file().parent.with_name("ctx-guard-scan") / (SID + ".scan")
        self.assertFalse(scan_path.exists())
        return proc

    def test_broken_compaction_import_keeps_hard_cap_guidance(self):
        proc = self.broken_compaction_hook("raise RuntimeError('synthetic import failure')\n")
        self.assertEqual(proc.stderr, "")

    def test_broken_compaction_scan_keeps_hard_cap_guidance(self):
        proc = self.broken_compaction_hook("def scan(*args):\n    raise RuntimeError('synthetic scan failure')\n")
        self.assertEqual(proc.stderr, "")

    def test_broken_compaction_debug_note(self):
        for body in ("raise RuntimeError('synthetic import failure')\n",
                     "def scan(*args):\n    raise RuntimeError('synthetic scan failure')\n"):
            with self.subTest(body=body):
                self.state_file().unlink(missing_ok=True)
                proc = self.broken_compaction_hook(body, AUTOOS_CTX_DEBUG="1")
                self.assertEqual(proc.stderr,
                                 "context_guard: context_compaction unavailable; continuing without scan\n")

    def test_deliberate_difference_persistence_failure_does_not_hide_warning(self):
        import io
        guard = load_module("context_guard")
        self.write(usage_line(inp=300000))
        payload = json.dumps({"transcript_path": str(self.transcript), "session_id": SID, "cwd": "/w/x"})
        with patch.dict(os.environ, self.env(), clear=True), patch.object(guard, "read_stdin_text", return_value=payload), \
                patch.dict(sys.modules, {"context_compaction": load_module("context_compaction")}), \
                patch.object(guard, "_write_json", side_effect=OSError("synthetic failure")), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(guard.run_hook(), 0)
        self.assertIn("HARD-CAP", out.getvalue())

    def test_deliberate_difference_keeps_calm_private_path_free_guidance(self):
        guard = load_module("context_guard")
        self.assertEqual(guard.emit_line("hard", 300000, 300000),
                         "HARD-CAP ctx=300k >= 300k: at the next natural stop, write your handoff to "
                         "your private handoff path, then run ready (and SendMessage RECYCLE-READY <path> "
                         "to your parent if your brief says so)." + guard.SUBAGENT_BACKSTOP)
