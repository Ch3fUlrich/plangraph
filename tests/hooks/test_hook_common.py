#!/usr/bin/env python3
"""Tests for tools/hooks/hook_common.py (the helpers the hooks share).

`SAFE_ID` is exported and other hooks import it by name, so its pattern is pinned here; `safe_id`, `state_home`
and the atomic writers are the behaviour every hook leans on.

    python3 tests/hooks/test_hook_common.py
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HookCase, load_module  # noqa: E402


class SafeId(HookCase):
    def setUp(self):
        super().setUp()
        self.common = load_module("hook_common")

    def test_safe_id_pattern_is_exported_unchanged(self):
        self.assertIsInstance(self.common.SAFE_ID, re.Pattern)
        self.assertEqual(self.common.SAFE_ID.pattern, r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

    def test_ids_that_are_safe_file_names(self):
        for good in ("a", "A1", "11111111-2222-3333-4444-555555555555", "a.b_c-d", "x" * 128, "0", "fake-parent"):
            self.assertTrue(self.common.safe_id(good), good)

    def test_ids_that_could_escape_or_hide(self):
        for bad in ("", ".hidden", "-x", "_x", "a/b", "a\\b", "..", "../evil", "x" * 129, "a b", "ab\n", "\u00e9",
                    "a\x00b"):
            self.assertFalse(self.common.safe_id(bad), repr(bad))

    def test_only_strings_are_ids(self):
        for bad in (None, 5, 5.0, True, b"abc", ["a"], {"a": 1}):
            self.assertFalse(self.common.safe_id(bad), repr(bad))


class StateHome(HookCase):
    def setUp(self):
        super().setUp()
        self.common = load_module("hook_common")

    def test_xdg_state_home_wins(self):
        self.patch_env(XDG_STATE_HOME=self.state)
        self.assertEqual(self.common.state_home(), self.state)

    def test_default_is_under_home(self):
        self.patch_env(XDG_STATE_HOME=None, HOME=self.home, USERPROFILE=self.home)
        self.assertEqual(self.common.state_home(), self.home / ".local" / "state")

    def test_an_empty_xdg_value_counts_as_unset(self):
        self.patch_env(XDG_STATE_HOME="", HOME=self.home, USERPROFILE=self.home)
        self.assertEqual(self.common.state_home(), self.home / ".local" / "state")


class AtomicWrites(HookCase):
    def setUp(self):
        super().setUp()
        self.common = load_module("hook_common")
        self.tmp.joinpath("out").mkdir()
        self.target = self.tmp / "out" / "f.json"

    def names(self):
        return sorted(p.name for p in self.target.parent.iterdir())

    def test_write_replaces_the_file_and_leaves_no_temp(self):
        self.common.atomic_write_text(self.target, "one")
        self.common.atomic_write_text(self.target, "two")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "two")
        self.assertEqual(self.names(), ["f.json"])

    def test_write_json_round_trips(self):
        doc = {"session_id": "s", "ctx": 301000, "cwd": "/w/\u00e9", "ok": True}
        self.common.write_json(self.target, doc)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), doc)
        self.assertEqual(self.names(), ["f.json"])

    def test_a_failed_replace_removes_the_temp_file(self):
        self.target.mkdir()  # os.replace of a file onto a directory fails
        with self.assertRaises(OSError):
            self.common.atomic_write_text(self.target, "x")
        self.assertEqual(self.names(), ["f.json"], "only the directory is left, no *.tmp")

    def test_a_missing_directory_raises_for_the_caller_to_handle(self):
        with self.assertRaises(OSError):
            self.common.atomic_write_text(self.tmp / "missing" / "f", "x")


if __name__ == "__main__":
    unittest.main(verbosity=1)
