"""Synthetic transcript fixtures; no operator transcript content."""
import json
import time
import io
import os
from unittest import mock
from datetime import datetime, timezone

from test_ctx_guard_session import WrapperCase
from hookkit import usage_line, load_module
from test_ctx_guard_session import SCRIPT


class Attended(WrapperCase):
    def guard(self, *args, **kwargs):
        self.state_file().unlink(missing_ok=True)  # independent emissions, not cooldown tests
        return super().guard(*args, **kwargs)

    def user(self, age=10, text="Synthetic operator instruction", **extra):
        return {"type": "user", "timestamp": datetime.fromtimestamp(
            time.time() - age, timezone.utc).isoformat(),
            "promptSource": "typed", "turnOrigin": "human", "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}, **extra}

    def transcript_records(self, *records):
        self.write("\n".join(json.dumps(r) for r in records) + "\n" + usage_line(read=301000))

    def marker(self):
        return self.state_file().with_suffix(".attended.json")

    def test_recent_and_marker_removed_when_old(self):
        operator = self.user()
        self.transcript_records(self.user(), operator)
        self.assert_silent(self.guard())
        self.assertEqual(json.loads(self.marker().read_text())["until"],
                         datetime.fromisoformat(operator["timestamp"]).timestamp() + 1800)
        self.assertEqual(self.marker().stat().st_mode & 0o777, 0o600)
        self.transcript_records(self.user(), self.user(age=4000))
        self.assertIn("HARD-CAP", self.line(self.guard()))
        self.assertFalse(self.marker().exists())

    def test_launch_only(self):
        for source in ("typed", "queued", "sdk"):
            self.transcript_records(self.user(promptSource=source))
            self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_queued_and_sdk_launch_with_operator_is_attended(self):
        for source in ("queued", "sdk"):
            with self.subTest(source=source):
                self.transcript_records(self.user(promptSource=source), self.user())
                self.assert_silent(self.guard())

    def test_machine_launch_with_human_metadata_is_rejected(self):
        for content in ('<cross-session-message>relay</cross-session-message>',
                        ' SYSTEM NOTIFICATION: synthetic',
                        [{'type': 'text', 'text': '<system-reminder>synthetic</system-reminder>'}],
                        [{'type': 'tool_result', 'content': 'synthetic'}]):
            with self.subTest(content=content):
                self.transcript_records(self.user(text=content), self.user())
                self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_tail_boundary_keeps_complete_line_and_drops_partial(self):
        module = load_module("ctx_guard_session")
        launch = json.dumps(self.user()) + "\n"
        operator = json.dumps(self.user()) + "\n"
        usage = usage_line(read=301000)
        padding = json.dumps({"type": "assistant", "padding": "x" * 4096}) + "\n"
        for midline in (False, True):
            with self.subTest(midline=midline):
                self.write(launch + padding + operator + usage)
                tail_size = len((operator + usage + "\n").encode()) - int(midline)
                with mock.patch.object(module, "ATTENDED_TAIL_MAX", tail_size):
                    until = module.attended_until(self.payload(), None)
                if midline:
                    self.assertIsNone(until)
                else:
                    self.assertIsNotNone(until)

    def test_first_user_must_be_plausible_launch(self):
        for extra in ({"promptSource": "system", "turnOrigin": "peer"},
                      {"turnOrigin": "task_notification"}, {"origin": {"kind": "peer"}},
                      {"promptSource": None}, {"isMeta": True}, {"isSidechain": True}):
            with self.subTest(extra=extra):
                self.transcript_records(self.user(**extra), self.user(), self.user())
                self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_origin_metadata_allowed_missing_origin_refused(self):
        self.transcript_records(self.user(), self.user(origin={"kind": "human", "source": "terminal"}))
        self.assert_silent(self.guard())
        record = self.user()
        del record["origin"]
        self.transcript_records(self.user(), self.user(age=4000), record)
        self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_lease_publication_failure_runs_guard(self):
        module = load_module("ctx_guard_session")
        self.transcript_records(self.user(), self.user())
        for target in ("Path.mkdir", "tempfile.mkstemp", "os.replace"):
            with self.subTest(target=target):
                self.state_file().unlink(missing_ok=True)
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state),
                                                  "AUTOOS_CTX_HARD": "300000"}), \
                        mock.patch.object(module, "read_stdin_text", return_value=json.dumps(self.payload())), \
                        mock.patch.object(module, "settings_thresholds", return_value={}), \
                        mock.patch.object(module.sys, "argv", [str(SCRIPT)]), \
                        mock.patch.object(module.sys, "stdout", output), \
                        mock.patch.object(module.sys, "stdin", io.StringIO()), \
                        mock.patch.object(module.Path if target == "Path.mkdir" else
                                          module.tempfile if target == "tempfile.mkstemp" else module.os,
                                          target.split(".")[1], side_effect=OSError("synthetic failure")):
                    self.assertEqual(module.main(), 0)
                self.assertIn("HARD-CAP", output.getvalue())
                self.assertFalse(self.marker().exists())
                self.assertEqual(list(self.marker().parent.glob(self.marker().name + ".*")), [])

    def large_transcript(self, *tail, first=None):
        padding = json.dumps({"type": "assistant", "padding": "x" * 8192}) + "\n"
        self.write(json.dumps(first or self.user()) + "\n" + padding * 384 +
                   "".join(json.dumps(r) + "\n" for r in tail) + usage_line(read=301000))

    def test_large_queued_tail_is_attended(self):
        self.large_transcript(self.user(age=60, promptSource="queued"))
        self.assert_silent(self.guard())
        self.assertTrue(self.marker().exists())

    def test_large_peer_tail_is_not_attended(self):
        self.large_transcript(self.user(promptSource="system", turnOrigin="peer"))
        self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_large_launch_only_is_not_attended(self):
        self.large_transcript()
        self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_queued_operator_is_attended(self):
        self.transcript_records(self.user(), self.user(promptSource="queued"))
        self.assert_silent(self.guard())

    def test_large_missing_launch_and_invalid_windows_fail_closed(self):
        self.large_transcript(self.user(), first={"type": "assistant"})
        self.assertIn("HARD-CAP", self.line(self.guard()))
        for where in ("head", "tail"):
            self.large_transcript(self.user(promptSource="queued"))
            text = self.transcript.read_text()
            self.write("{broken\n" + text if where == "head" else text + "{broken\n")
            self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_relays_and_machine_records_cannot_lift_cap(self):
        records = [self.user(text=t) for t in (
            '<cross-session-message>synthetic</cross-session-message>',
            '<system-reminder>synthetic</system-reminder>',
            '  <user-prompt-submit-hook>synthetic</user-prompt-submit-hook>',
            '\n <arbitrary-wrapper key="value">synthetic</arbitrary-wrapper>',
            'SYSTEM NOTIFICATION: synthetic', '<task-notification>synthetic</task-notification>',
            '<command-output>synthetic</command-output>', '<local-command-stdout>synthetic</local-command-stdout>')]
        records += [self.user(isMeta=True), self.user(isSidechain=True), self.user(promptSource="sdk"),
                    self.user(origin={"kind": "peer"}), self.user(origin={}), self.user(origin=None),
                    self.user(promptSource="system", turnOrigin="peer"),
                    self.user(promptSource="system", turnOrigin="task_notification"),
                    self.user(message={"role": "user", "content": [{"type": "tool_result", "content": "synthetic"}]})]
        for record in records:
            with self.subTest(record=record):
                self.transcript_records(self.user(), self.user(age=4000), record)
                self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_middle_quote_counts(self):
        self.transcript_records(self.user(), self.user(text='Please inspect <system-reminder>synthetic</system-reminder>'))
        self.assert_silent(self.guard())

    def test_text_blocks_and_unknown_provenance(self):
        self.transcript_records(self.user(), self.user(message={"role": "user", "content": [
            {"type": "text", "text": "Synthetic typed block"}]}))
        self.assert_silent(self.guard())
        record = self.user()
        del record["promptSource"]
        self.transcript_records(self.user(), record)
        self.assertIn("HARD-CAP", self.line(self.guard()))
        self.assertFalse(self.marker().exists())

    def test_missing_transcript_removes_marker_and_runs_guard(self):
        self.transcript_records(self.user(), self.user())
        self.assert_silent(self.guard())
        self.transcript.unlink()
        self.assert_silent(self.guard())  # unchanged guard has no usage to act on
        self.assertFalse(self.marker().exists())

    def test_invalid_and_oversized_fail_closed(self):
        for record in (self.user(timestamp="bad"), self.user(age=-100), self.user(message={"content": 42})):
            self.transcript_records(self.user(), record)
            self.assertIn("HARD-CAP", self.line(self.guard()))
        for prefix in ('{broken\n', 'x' * 1048577 + '\n'):
            self.write(prefix + usage_line(read=301000))
            self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_window_off_and_unattended_thresholds(self):
        self.set_override({"attended_window_s": 5, "handoff": 50000, "hard": 90000})
        self.transcript_records(self.user(), self.user(age=10))
        self.assertIn("HARD-CAP ctx=301k >= 90k", self.line(self.guard()))
        self.set_override({"off": True})
        self.assert_silent(self.guard())
