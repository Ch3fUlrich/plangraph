"""Synthetic transcript fixtures; no operator transcript content."""
import json
import time
from datetime import datetime, timezone

from test_ctx_guard_session import WrapperCase
from hookkit import usage_line


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
        self.transcript_records(self.user(), self.user())
        self.assert_silent(self.guard())
        self.assertAlmostEqual(json.loads(self.marker().read_text())["until"], time.time()+1790, delta=5)
        self.assertEqual(self.marker().stat().st_mode & 0o777, 0o600)
        self.transcript_records(self.user(), self.user(age=4000))
        self.assertIn("HARD-CAP", self.line(self.guard()))
        self.assertFalse(self.marker().exists())

    def test_launch_only(self):
        self.transcript_records(self.user())
        self.assertIn("HARD-CAP", self.line(self.guard()))

    def test_relays_and_machine_records_cannot_lift_cap(self):
        records = [self.user(text=t) for t in (
            '<cross-session-message>synthetic</cross-session-message>',
            '<system-reminder>synthetic</system-reminder>',
            'SYSTEM NOTIFICATION: synthetic', '<task-notification>synthetic</task-notification>',
            '<command-output>synthetic</command-output>', '<local-command-stdout>synthetic</local-command-stdout>')]
        records += [self.user(isMeta=True), self.user(promptSource="sdk"),
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
