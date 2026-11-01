#!/usr/bin/env python3
"""Replay the recorded hook parity corpus against the ported hooks.

``parity_payloads.json`` is the contract: ~30 recorded stdin payloads for the
original hooks (``bash_guard`` allowed/blocked/empty/malformed/non-Bash,
``fleet_inject`` UserPromptSubmit/PostToolUse, ``fleet_report`` Stop,
``context_guard`` PostToolUse) with the exact stdout, stderr and exit code
they produced, captured by running the ORIGINAL hooks under
``/home/s/code/AutoOS/tools/hooks/`` in a temp HOME with empty state (see
``_parity.py`` for the capture protocol - each scenario was run twice and
kept only when both runs were byte-identical, so nothing time-, pid- or
host-dependent is recorded).

Each test here replays one recorded payload on the PORT hooks in
``tools/hooks/`` - the split ``bash_guard.py`` + ``bash_guard_rules*`` and
its siblings - inside its own fresh temp HOME with empty state, and asserts
stdout and stderr byte for byte plus the exit code. Any drift between the
originals and the port fails exactly one payload's test.

Payloads whose output could not be made stable are listed under ``skipped``
in the corpus with both captured runs kept for the record - that is the
emitting context-guard payloads, whose additionalContext embeds the hook's
own resolved install path (host-dependent by construction).
"""
import json
import re
import tempfile
import unittest

import _parity

CORPUS = json.loads(_parity.CORPUS.read_text(encoding="utf-8"))
PAYLOADS = CORPUS["payloads"]
SKIPPED = CORPUS["skipped"]


class ParityPayload(unittest.TestCase):
    """One generated test per recorded payload; expected output is byte-exact."""

    maxDiff = None


def _make_parity_test(record):
    def test(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, stderr, code = _parity.run_scenario(_parity.PORT_HOOKS, record, tmp)
        self.assertEqual(code, record["exit"],
                         "exit code drifted for %s (stdout=%r stderr=%r)"
                         % (record["name"], stdout, stderr))
        self.assertEqual(stdout, record["stdout"].encode("utf-8"),
                         "stdout must be byte-identical to the original hook's")
        self.assertEqual(stderr, record["stderr"].encode("utf-8"),
                         "stderr must be byte-identical to the original hook's")
    return test


for _record in PAYLOADS:
    _test_name = "test_" + re.sub(r"\W", "_", _record["name"])
    setattr(ParityPayload, _test_name, _make_parity_test(_record))


class CorpusHygiene(unittest.TestCase):
    """The corpus itself must stay a fair witness of the capture."""

    def test_every_payload_has_the_three_recorded_fields(self):
        for record in PAYLOADS:
            for key in ("name", "hook", "stdin", "stdout", "stderr", "exit"):
                self.assertIn(key, record, record.get("name"))
            self.assertIsInstance(record["exit"], int, record["name"])
        names = [record["name"] for record in PAYLOADS]
        self.assertEqual(len(names), len(set(names)), "payload names are unique")

    def test_every_hook_family_is_covered(self):
        hooks = {record["hook"] for record in PAYLOADS}
        self.assertEqual(hooks, {"bash_guard", "fleet_inject", "fleet_report",
                                 "context_guard"})
        events = [record["stdin"] for record in PAYLOADS if record["hook"] == "fleet_inject"]
        self.assertTrue(any("UserPromptSubmit" in s for s in events))
        self.assertTrue(any("PostToolUse" in s for s in events))

    def test_recorded_set_is_the_expected_size(self):
        self.assertGreaterEqual(len(PAYLOADS), 20, "about twenty payloads are recorded")

    def test_skipped_payloads_keep_their_reason_and_both_runs(self):
        self.assertGreaterEqual(len(SKIPPED), 1, "the skip rule left a written record")
        for record in SKIPPED:
            self.assertIn("reason", record, record.get("name"))
            self.assertIn("run_a", record, record.get("name"))
            self.assertIn("run_b", record, record.get("name"))

    def test_replay_runs_the_port_hooks(self):
        for record in PAYLOADS:
            script = _parity.PORT_HOOKS / ("%s.py" % record["hook"])
            self.assertTrue(script.is_file(), script)


if __name__ == "__main__":
    unittest.main(verbosity=1)
