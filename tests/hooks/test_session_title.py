#!/usr/bin/env python3
"""Tests for tools/hooks/session_title.py (counter titles for unnamed sessions).

The hook is a UserPromptSubmit command: hook JSON on stdin (`session_id`, `cwd`, `transcript_path`, `prompt`),
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "<cwd name>-NNNN"}}` on stdout.
Everything runs in a temp HOME and state dir (hookkit); nothing reads the live machine. Run directly or through
pytest:

    python3 tests/hooks/test_session_title.py
"""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, SID, HookCase, load_module  # noqa: E402

SCRIPT = HOOKS / "session_title.py"
FOUR_DIGITS = re.compile(r"-\d{4}\Z")


class SeqCase(HookCase):
    """The shared counter file under an isolated ROUTING_STATE_DIR, driven in-process."""

    def setUp(self):
        super().setUp()
        self.mod = load_module("session_title")
        self.seq_dir = self.tmp / "routing"
        self.seq_file = self.seq_dir / "session-seq"
        self.patch_env(ROUTING_STATE_DIR=self.seq_dir)

    def stored(self, text):
        self.seq_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            self.seq_file.write_bytes(text)
        else:
            self.seq_file.write_text(text, encoding="utf-8")

    def counter_text(self):
        return self.seq_file.read_text(encoding="utf-8")


class CounterRange(SeqCase):
    """H1: a counter of 0..N counts on by one; garbled input never yields a 10-digit name."""

    def test_h1_missing_or_empty_file_starts_at_one(self):
        self.assertEqual(self.mod.next_seq(), 1)
        self.assertEqual(self.counter_text(), "1")
        for text in ("", "  \n"):   # without a mark an empty counter starts over
            self.seq_file.with_name("session-seq.last").unlink()
            self.stored(text)
            self.assertEqual(self.mod.next_seq(), 1)

    def test_h1_a_truncated_or_garbled_counter_continues_after_the_last_issued_value(self):
        for text in ("", "garbled", "1730000000"):
            self.stored("4999")
            first = self.mod.next_seq()
            self.stored(text)   # e.g. a crash between truncate and write
            self.assertEqual((first, self.mod.next_seq()), (5000, 5001), repr(text))

    def test_h1_a_marked_value_is_never_issued_again_after_a_lost_counter_write(self):
        self.stored("4999")
        self.assertEqual(self.mod.next_seq(), 5000)
        self.stored("4999")   # crash after the mark write, before the counter write
        self.assertEqual(self.mod.next_seq(), 5001)
        self.seq_file.with_name("session-seq.last").write_bytes(b"\xff\xfe")   # a non-UTF-8 mark is no mark
        self.assertEqual(self.mod.next_seq(), 5002)

    def test_h1_the_ring_wraps_9999_to_1_and_never_reissues_9999(self):
        self.stored("9998")
        self.assertEqual([self.mod.next_seq() for _ in range(3)], [9999, 1, 2])

    def test_h1_zero_is_a_valid_counter(self):
        self.stored("0")
        self.assertEqual(self.mod.next_seq(), 1, "a stored 0 counts on to 1, it is not garbled")
        self.assertEqual(self.counter_text(), "1")

    def test_h1_every_counter_value_counts_on_by_one(self):
        for stored, want in (("1", 2), ("41", 42), ("0007", 8), (" 99 \n", 100), ("9998", 9999), ("9999", 1)):
            self.stored(stored)
            self.assertEqual(self.mod.next_seq(), want, repr(stored))
            self.assertEqual(self.counter_text(), str(want))

    def test_h1_consecutive_calls_count_up(self):
        self.assertEqual([self.mod.next_seq() for _ in range(5)], [1, 2, 3, 4, 5])

    def test_h1_garbled_counters_never_leave_four_digits(self):
        garbled = ["abc", "-3", "-0", "+5", "1.5", "0x10", "1 2", "10000", "1730000000",
                   "9" * 5000, "\u0663", "\x00\x00", "{\"n\": 1}"]
        prev = None
        for text in garbled:
            self.stored(text)
            seq = self.mod.next_seq()
            self.assertTrue(1000 <= seq <= 9999 if prev is None else seq == prev % 9999 + 1, "%r -> %r" % (text[:20], seq))
            prev = seq
            self.assertEqual(self.counter_text(), str(seq), "the shared counter keeps a four-digit value")
            self.assertRegex(self.mod.title("/w/x", seq), FOUR_DIGITS)

    def test_h1_non_utf8_counter_file_is_garbled_not_fatal(self):
        self.stored(b"\xff\xfe\x00garbage")
        with mock.patch.object(self.mod, "time", mock.Mock(time=lambda: 1_700_000_000.0)):
            seq = self.mod.next_seq()
        self.assertEqual(seq, 9000)
        self.assertEqual(self.mod.next_seq(), 9001, "it counts on from the recovery value")

    def test_h1_recovery_is_clock_derived_and_counts_on_from_there(self):
        self.stored("garbled")
        with mock.patch.object(self.mod, "time", mock.Mock(time=lambda: 1_700_000_000.0)):
            first = self.mod.next_seq()
        self.assertEqual(first, 1_700_000_000 % 9000 + 1000)
        self.assertEqual(self.mod.next_seq(), first + 1)

    def test_h1_default_counter_lives_under_the_state_home(self):
        self.patch_env(ROUTING_STATE_DIR=None, XDG_STATE_HOME=self.state)
        self.assertEqual(self.mod.next_seq(), 1)
        self.assertEqual((self.state / "routing" / "session-seq").read_text(encoding="utf-8"), "1")


class TitleText(SeqCase):
    """H2: the title is the directory name and the counter, never prompt text."""

    def test_h2_title_is_cwd_name_and_counter_only(self):
        self.assertEqual(self.mod.title("/w/build-L1", 7), "build-L1-0007")
        self.assertEqual(self.mod.title("/w/x", 12345), "x-12345", "past four digits the number just grows")

    def test_name_is_cleaned_capped_and_defaulted(self):
        self.assertEqual(self.mod.title("/w/my proj", 4), "my-proj-0004")
        self.assertEqual(self.mod.title("/w/" + "n" * 80, 4), "n" * 30 + "-0004")
        for cwd in (None, "", "/", 5, ["a"]):
            self.assertEqual(self.mod.title(cwd, 4), "session-0004", repr(cwd))


class HookRuns(HookCase):
    """The hook end to end: one process per prompt, as Claude Code runs it."""

    def payload(self, sid=SID, **extra):
        doc = {"session_id": sid, "cwd": "/w/build-L1", "transcript_path": str(self.transcript),
               "prompt": "please rotate the deploy credentials for the staging cluster"}
        doc.update(extra)
        return doc

    def run_session(self, payload=None, **env):
        return self.run_hook(SCRIPT, payload if payload is not None else self.payload(), **env)

    def title_of(self, proc):
        self.assertEqual((proc.returncode, proc.stderr), (0, ""), proc.stderr)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        return out["sessionTitle"]

    def marker(self, sid=SID):
        return self.state / "claude-hooks" / "session-title" / sid

    def test_h1_first_prompt_gets_a_numbered_title_and_the_marker(self):
        self.assertEqual(self.title_of(self.run_session()), "build-L1-0001")
        self.assertTrue(self.marker().is_file())
        self.assertEqual((self.state / "routing" / "session-seq").read_text(encoding="utf-8"), "1")

    def test_h1_garbled_shared_counter_still_yields_a_four_digit_name(self):
        seq = self.state / "routing" / "session-seq"
        seq.parent.mkdir(parents=True)
        seq.write_text("not a number", encoding="utf-8")
        title = self.title_of(self.run_session())
        self.assertRegex(title, r"\Abuild-L1-\d{4}\Z")
        self.assertEqual(seq.read_text(encoding="utf-8"), title.rsplit("-", 1)[1].lstrip("0"))

    def test_h1_stored_zero_counts_on_to_one(self):
        seq = self.state / "routing" / "session-seq"
        seq.parent.mkdir(parents=True)
        seq.write_text("0", encoding="utf-8")
        self.assertEqual(self.title_of(self.run_session()), "build-L1-0001")

    def test_h2_no_prompt_word_reaches_the_title(self):
        prompt = "please rotate the deploy credentials for the staging cluster"
        title = self.title_of(self.run_session(self.payload(prompt=prompt)))
        self.assertEqual(title, "build-L1-0001")
        words = set(re.findall(r"[a-z0-9]+", prompt.lower()))
        self.assertEqual(words & set(re.findall(r"[a-z0-9]+", title.lower())), set())

    def test_h2_prompt_field_is_never_needed(self):
        for prompt in (None, 5, ["x"], "x " * 5000):
            doc = self.payload(sid="s-%s" % type(prompt).__name__, prompt=prompt)
            self.assertRegex(self.title_of(self.run_session(doc)), r"\Abuild-L1-\d{4}\Z")
        doc = self.payload(sid="s-absent")
        del doc["prompt"]
        self.assertRegex(self.title_of(self.run_session(doc)), r"\Abuild-L1-\d{4}\Z")

    def test_second_prompt_of_a_titled_session_is_silent(self):
        self.title_of(self.run_session())
        again = self.run_session()
        self.assertEqual((again.returncode, again.stdout), (0, ""))
        self.assertEqual((self.state / "routing" / "session-seq").read_text(encoding="utf-8"), "1")

    def test_sessions_take_consecutive_numbers(self):
        titles = [self.title_of(self.run_session(self.payload(sid="s%d" % i))) for i in range(3)]
        self.assertEqual(titles, ["build-L1-0001", "build-L1-0002", "build-L1-0003"])

    def test_session_with_a_custom_title_is_left_alone(self):
        self.write(json.dumps({"type": "user", "message": {"content": "hi"}}),
                   json.dumps({"type": "custom-title", "customTitle": "build-L1-0412"}))
        proc = self.run_session()
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertTrue(self.marker().is_file(), "an already-titled session is settled")
        self.assertFalse((self.state / "routing" / "session-seq").exists(), "no counter value is consumed")

    def test_custom_title_is_found_inside_the_tail_window_only(self):
        title_line = json.dumps({"type": "custom-title", "customTitle": "named"}) + "\n"
        filler = (json.dumps({"type": "user", "message": {"content": "x" * 400}}) + "\n") * 400  # ~160 KB
        self.transcript.write_text(filler + title_line, encoding="utf-8")
        self.assertEqual(self.run_session().stdout, "", "the title is in the last 128 KB")
        self.transcript.write_text(title_line + filler, encoding="utf-8")
        self.assertIn("sessionTitle", self.run_session(self.payload(sid="s2")).stdout,
                      "a title before the tail window is the documented miss")

    def test_unsafe_session_ids_write_nothing(self):
        for bad in ("../evil", "a/b", "..", ".hidden", "x" * 200, "a\\b", "", None, 5):
            proc = self.run_session(self.payload(sid=bad))
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), repr(bad))
        self.assertEqual([p for p in self.state.rglob("*") if p.is_file()], [], "no state written for a bad id")

    def test_garbled_stdin_is_silent(self):
        for raw in ("{not json", "", "[1, 2]", "null", '"s"'):
            proc = self.run_session(raw)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), repr(raw))

    def test_failed_run_retries_on_the_next_prompt(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        broken = self.run_session(ROUTING_STATE_DIR=blocker / "sub")
        self.assertEqual((broken.returncode, broken.stdout), (0, ""), "fail-safe: never blocks the prompt")
        self.assertFalse(self.marker().exists(), "no marker without a title")
        self.assertEqual(self.title_of(self.run_session()), "build-L1-0001")

    def test_missing_transcript_still_gets_a_title(self):
        self.assertEqual(self.title_of(self.run_session(self.payload(transcript_path="/no/such/file"))),
                         "build-L1-0001")
        self.assertEqual(self.title_of(self.run_session(self.payload(sid="s2", transcript_path=None))),
                         "build-L1-0002")


if __name__ == "__main__":
    unittest.main(verbosity=1)
