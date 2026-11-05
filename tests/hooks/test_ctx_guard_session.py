#!/usr/bin/env python3
"""Tests for tools/hooks/ctx_guard_session.py (per-session thresholds in front of context_guard.py).

The wrapper is the PostToolUse hook: hook JSON on stdin (`transcript_path`, `session_id`, `cwd`), the guard's JSON
line on stdout with the fleet-handoff card rule appended. A file `<state>/claude-hooks/ctx-guard-session/<id>.json`
holding `{"handoff": N, "hard": M}` or `{"off": true}` changes (or skips) the guard for that one session (H6); the
thresholds in Claude Code's settings files are layered underneath. The AUTOOS_CTX_* names and the
`autoos/ctx-guard` state dir are the live guard's contract and stay until the cutover item G13.

    python3 tests/hooks/test_ctx_guard_session.py
"""
import json
import shutil
import sys
from unittest import mock
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, SID, HookCase, load_module, usage_line  # noqa: E402

SCRIPT = HOOKS / "ctx_guard_session.py"


class WrapperCase(HookCase):
    def setUp(self):
        super().setUp()
        self.cwd = self.home / "proj" / "sub"  # under HOME: the settings walk stops there
        self.cwd.mkdir(parents=True)

    def payload(self, sid=SID, **extra):
        doc = {"transcript_path": str(self.transcript), "session_id": sid, "cwd": str(self.cwd)}
        doc.update(extra)
        return doc

    def override_file(self, sid=SID):
        return self.state / "claude-hooks" / "ctx-guard-session" / ("%s.json" % sid)

    def set_override(self, doc, sid=SID):
        path = self.override_file(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    def settings(self, directory, env, name="settings.json"):
        path = Path(directory) / ".claude" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env if isinstance(env, str) else json.dumps({"env": env}), encoding="utf-8")

    def guard(self, payload=None, **env):
        return self.run_hook(SCRIPT, self.payload() if payload is None else payload, **env)

    def line(self, proc):
        self.assertEqual((proc.returncode, proc.stderr), (0, ""), proc.stderr)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def assert_silent(self, proc):
        self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr)

    def state_file(self, sid=SID):
        return self.state / "autoos" / "ctx-guard" / ("%s.json" % sid)


class OverrideFile(WrapperCase):
    """H6: the per-session file format `{"handoff": N, "hard": M}` / `{"off": true}` keeps working."""

    def test_h6_no_override_runs_the_guard_unchanged(self):
        self.write(usage_line(read=275000))
        self.assertTrue(self.line(self.guard()).startswith("HANDOFF-NOW ctx=275k >= 270k"))
        self.write(usage_line(read=60000))
        self.assert_silent(self.guard(self.payload(sid="other")))

    def test_h6_handoff_and_hard_replace_the_defaults(self):
        self.write(usage_line(read=60000))
        self.set_override({"handoff": 50000, "hard": 90000})
        self.assertTrue(self.line(self.guard()).startswith("HANDOFF-NOW ctx=60k >= 50k"))
        self.set_override({"handoff": 10000, "hard": 55000}, sid="s2")
        self.assertTrue(self.line(self.guard(self.payload(sid="s2"))).startswith("HARD-CAP ctx=60k >= 55k"))

    def test_h6_one_key_overrides_only_itself(self):
        self.write(usage_line(read=60000))
        self.set_override({"handoff": 50000})
        self.assertTrue(self.line(self.guard()).startswith("HANDOFF-NOW ctx=60k >= 50k"))
        self.set_override({"hard": 90000}, sid="s2")
        self.assert_silent(self.guard(self.payload(sid="s2")))  # handoff stays 270000

    def test_h6_off_skips_the_guard_for_that_session_only(self):
        self.write(usage_line(read=301000))
        self.set_override({"off": True})
        self.assert_silent(self.guard())
        self.assertFalse(self.state_file().exists(), "an off session leaves no guard state")
        self.assertIn("HARD-CAP", self.line(self.guard(self.payload(sid="other"))))

    def test_h6_off_must_be_the_json_true(self):
        self.write(usage_line(read=301000))
        for n, off in enumerate((1, "true", False, None, [True])):
            self.set_override({"off": off}, sid="s%d" % n)
            self.assertIn("HARD-CAP", self.line(self.guard(self.payload(sid="s%d" % n))), repr(off))

    def test_h6_only_plain_integers_count(self):
        self.write(usage_line(read=60000))
        for n, doc in enumerate(({"handoff": True}, {"handoff": "50000"}, {"handoff": 50000.0}, {"handoff": None},
                                 {"handoff": [50000]}, {"hard": False})):
            self.set_override(doc, sid="s%d" % n)
            self.assert_silent(self.guard(self.payload(sid="s%d" % n)))

    def test_h6_garbled_or_non_object_file_is_ignored(self):
        self.write(usage_line(read=60000))
        for n, raw in enumerate(("{nope", "[1, 2]", "null", "", '"s"', "5", "{}")):
            self.set_override(raw, sid="s%d" % n)
            self.assert_silent(self.guard(self.payload(sid="s%d" % n)))
        self.write(usage_line(read=275000))
        self.set_override("{nope", sid="late")
        self.assertIn("HANDOFF-NOW", self.line(self.guard(self.payload(sid="late"))))

    def test_h6_override_beats_the_settings_env(self):
        self.write(usage_line(read=60000))
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "40000", "AUTOOS_CTX_HARD": "90000"})
        self.set_override({"handoff": 55000})
        self.assertTrue(self.line(self.guard()).startswith("HANDOFF-NOW ctx=60k >= 55k"))

    def test_h6_state_dir_is_the_live_guards(self):
        self.write(usage_line(read=275000))
        self.guard()
        self.assertTrue(self.state_file().is_file(), "the guard state stays under autoos/ctx-guard")


class SettingsLayers(WrapperCase):
    """The thresholds are re-read on every call, layered as Claude Code layers settings."""

    def setUp(self):
        super().setUp()
        self.write(usage_line(read=60000))

    def handoff_of(self, proc):
        return self.line(proc).split(" >= ")[1].split(":")[0]

    def test_home_settings_env_applies(self):
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "50000", "AUTOOS_CTX_HARD": "90000"})
        self.assertEqual(self.handoff_of(self.guard()), "50k")

    def test_project_settings_beat_home_and_local_beats_project(self):
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "50000"})
        self.settings(self.home / "proj", {"AUTOOS_CTX_HANDOFF": "45000"})
        self.assertEqual(self.handoff_of(self.guard()), "45k", "cwd's parent project dir wins over home")
        self.settings(self.home / "proj", {"AUTOOS_CTX_HANDOFF": "42000"}, name="settings.local.json")
        self.assertEqual(self.handoff_of(self.guard(self.payload(sid="s2"))), "42k")

    def test_nearest_project_dir_wins(self):
        self.settings(self.home / "proj", {"AUTOOS_CTX_HANDOFF": "45000"})
        self.settings(self.cwd, {"AUTOOS_CTX_HANDOFF": "41000"})
        self.assertEqual(self.handoff_of(self.guard()), "41k")

    def test_settings_replace_the_process_env_of_an_old_session(self):
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "50000"})
        self.assertEqual(self.handoff_of(self.guard(AUTOOS_CTX_HANDOFF="270000")), "50k")

    def test_process_env_applies_without_settings(self):
        self.assertEqual(self.handoff_of(self.guard(AUTOOS_CTX_HANDOFF="50000")), "50k")

    def test_each_key_is_parsed_on_its_own(self):
        self.write(usage_line(read=275000))
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "lots", "AUTOOS_CTX_HARD": "280000"})
        self.assertTrue(self.line(self.guard()).startswith("HANDOFF-NOW ctx=275k >= 270k"),
                        "a garbled handoff drops itself")
        self.write(usage_line(read=60000))
        self.settings(self.home, {"AUTOOS_CTX_HANDOFF": "50000", "AUTOOS_CTX_HARD": "x"})
        self.assertTrue(self.line(self.guard(self.payload(sid="s2"))).startswith("HANDOFF-NOW ctx=60k >= 50k"))

    def test_garbled_settings_files_are_ignored(self):
        self.settings(self.home, "{nope")
        self.settings(self.home / "proj", '["env"]')
        self.write(usage_line(read=275000))
        self.assertIn("HANDOFF-NOW ctx=275k >= 270k", self.line(self.guard()))


class Plumbing(WrapperCase):
    def test_nul_destination_uses_default_guidance(self):
        self.write(usage_line(read=301000))
        self.set_override({"handoff_path": "/a\x00b"})
        text = self.line(self.guard())
        self.assertIn("HARD-CAP ctx=301k >= 300k", text)
        self.assertIn("your private handoff path", text)

    def test_set_command_writes_atomically_and_isolates(self):
        wrapper = load_module("ctx_guard_session")
        with mock.patch.object(wrapper, "overrides_dir", return_value=self.override_file().parent), \
                mock.patch.object(sys, "argv", [str(SCRIPT), "--set", SID, "320000", "330000"]), \
                mock.patch("hook_common.os.replace", wraps=__import__("os").replace) as replace:
            self.assertEqual(wrapper.main(), 0)
            replace.assert_called_once()
        self.assertEqual(json.loads(self.override_file().read_text()), {"handoff": 320000, "hard": 330000})
        self.write(usage_line(read=301000))
        self.assert_silent(self.guard())
        self.assertIn("HARD-CAP ctx=301k >= 300k", self.line(self.guard(self.payload(sid="other"))))
        before = self.override_file().read_bytes()
        for sid, handoff, hard in (("../bad", "1", "2"), (SID, "0", "2"), (SID, "2", "2"),
                                   (SID, "3", "2"), (SID, "bad", "4"), (SID, "-1", "2")):
            with mock.patch.object(sys, "argv", [str(SCRIPT), "--set", sid, handoff, hard]):
                self.assertEqual(wrapper.main(), 2)
        self.assertEqual(self.override_file().read_bytes(), before)

    def test_calm_text_at_both_levels_and_private_destination(self):
        for n, read in enumerate((275000, 301000)):
            self.write(usage_line(read=read))
            for configured in (False, True):
                sid = "calm%d%s" % (n, configured)
                path = str(self.tmp / "private" / "handoff.md")
                if configured:
                    self.set_override({"handoff_path": path}, sid=sid)
                text = self.line(self.guard(self.payload(sid=sid)))
                self.assertIn("HARD-CAP" if n else "HANDOFF-NOW", text)
                self.assertIn("ctx=%dk" % (read // 1000), text)
                self.assertIn("at the next natural stop, write your handoff to %s, then run" %
                              (path if configured else "your private handoff path"), text)
                for forbidden in ("commit+push", "no new work", "plangraph"):
                    self.assertNotIn(forbidden, text)
                self.assertTrue(text.endswith("If you are a subagent, finish your task and report; "
                                              "this does not apply to you."))

    def test_subagents_neither_emit_nor_touch_state(self):
        for script in (SCRIPT, HOOKS / "context_guard.py"):
            for read in (1000, 275000, 301000):
                self.write(usage_line(read=read))
                self.assert_silent(self.run_hook(script, self.payload(agent_id="worker")))
                self.assertFalse(self.state_file().exists())
                self.state_file().parent.mkdir(parents=True, exist_ok=True)
                self.state_file().write_text('{"ctx": 290000}', encoding="utf-8")
                self.assert_silent(self.run_hook(script, self.payload(agent_id="worker")))
                self.assertEqual(self.state_file().read_text(), '{"ctx": 290000}')
                self.state_file().unlink()

    def test_launch_override_raises_lowers_isolates_and_clamps(self):
        self.write(usage_line(read=301000))
        self.set_override({"handoff": 320000, "hard": 330000})
        self.assert_silent(self.guard())
        self.assertIn("HARD-CAP ctx=301k >= 300k", self.line(self.guard(self.payload(sid="other"))))
        self.set_override({"handoff": 290000, "hard": 1})
        self.assertIn("HARD-CAP ctx=301k >= 290k", self.line(self.guard()))
        self.set_override("{broken", sid="garbled")
        self.assertIn("HARD-CAP ctx=301k >= 300k", self.line(self.guard(self.payload(sid="garbled"))))

    def test_card_rule_is_appended_to_both_levels(self):
        card = load_module("ctx_guard_session").CARD_RULE
        for sid, read, head in (("s1", 275000, "HANDOFF-NOW"), ("s2", 301000, "HARD-CAP")):
            self.write(usage_line(read=read))
            proc = self.guard(self.payload(sid=sid))
            text = self.line(proc)
            self.assertTrue(text.startswith(head) and card in text, text)
            self.assertEqual(len(proc.stdout.splitlines()), 1, "still one JSON line")
            self.assertTrue(proc.stdout.endswith("\n"))

    def test_add_card_rule_touches_only_the_threshold_lines(self):
        mod = load_module("ctx_guard_session")
        line = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                  "additionalContext": "HARD-CAP ctx=301k"}})
        added = json.loads(mod.add_card_rule(line))["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(added.endswith(mod.CARD_RULE))
        for other in ("", "not json", "[1]", "{}", '{"hookSpecificOutput": {}}',
                      '{"hookSpecificOutput": {"additionalContext": "something else"}}',
                      '{"hookSpecificOutput": {"additionalContext": 5}}'):
            self.assertEqual(mod.add_card_rule(other), other, repr(other))

    def test_unsafe_session_ids_never_read_a_file_outside_the_override_dir(self):
        mod = load_module("ctx_guard_session")
        self.patch_env(XDG_STATE_HOME=self.state)
        (self.state / "claude-hooks" / "ctx-guard-session").mkdir(parents=True)  # so a `..` id could resolve
        (self.state / "claude-hooks" / "x.json").write_text('{"off": true}', encoding="utf-8")
        for bad in ("../x", "a/b", "..", ".hidden", "x" * 129, "a\\b", "ab\n", "", None, 5):
            self.assertIsNone(mod.session_override(json.dumps({"session_id": bad})), repr(bad))
        self.set_override({"off": True}, sid="a" * 128)
        self.assertEqual(mod.session_override(json.dumps({"session_id": "a" * 128})), {"off": True})

    def test_the_id_check_and_state_home_come_from_hook_common(self):
        mod = load_module("ctx_guard_session")
        self.assertEqual((mod.safe_id.__module__, mod.state_home.__module__), ("hook_common", "hook_common"))
        self.assertFalse(hasattr(mod, "_SAFE_ID"), "no private copy of the id pattern")

    def test_overrides_dir_follows_the_shared_state_home(self):
        mod = load_module("ctx_guard_session")
        self.patch_env(XDG_STATE_HOME=self.state)
        self.assertEqual(mod.overrides_dir(), self.state / "claude-hooks" / "ctx-guard-session")
        self.patch_env(XDG_STATE_HOME=None, HOME=self.home, USERPROFILE=self.home)
        self.assertEqual(mod.overrides_dir(), self.home / ".local" / "state" / "claude-hooks" / "ctx-guard-session")

    def test_garbled_stdin_is_silent(self):
        for raw in ("{not json", "", "[1, 2]", "null"):
            self.assert_silent(self.guard(raw))

    def test_a_hook_dir_without_hook_common_stays_silent_and_exits_zero(self):
        alone = self.tmp / "alone-common"
        alone.mkdir()
        shutil.copy(SCRIPT, alone)
        shutil.copy(HOOKS / "context_guard.py", alone)
        self.write(usage_line(read=301000))
        proc = self.run_hook(alone / SCRIPT.name, self.payload())
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("hook_common.py missing", proc.stderr)

    def test_a_hook_dir_without_the_guard_stays_silent_and_exits_zero(self):
        alone = self.tmp / "alone"
        alone.mkdir()
        shutil.copy(SCRIPT, alone)
        shutil.copy(HOOKS / "hook_common.py", alone)
        self.write(usage_line(read=301000))
        proc = self.run_hook(alone / SCRIPT.name, self.payload())
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("context_guard.py missing or broken", proc.stderr)
        (alone / "context_guard.py").write_text("def main(:\n", encoding="utf-8")  # present but unusable
        proc = self.run_hook(alone / SCRIPT.name, self.payload())
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("context_guard.py missing or broken", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
