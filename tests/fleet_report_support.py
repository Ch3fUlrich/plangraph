"""Shared pieces of tests/hooks/test_fleet_report_*.py and test_redact_common.py.

The hook modules sit in tools/hooks and import one another by bare name, so
the ``hooks`` fixture puts tools/ (through ``tools_importable``, the one way
tests reach tools/) and tools/hooks on sys.path for one test only. Every
secret is synthetic; ``assert_no_leak`` (autoos_refs_support) fails without
printing a value.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

SID, CHILD, PARENT = "sess-1", "child-0001", "parent-0001"


@pytest.fixture
def hooks(tools_importable, monkeypatch):
    """``(fleet_report, fleet_inject, hook_common, redact_common)``, imported."""
    monkeypatch.syspath_prepend(str(Path(tools_importable) / "hooks"))
    import fleet_inject
    import fleet_report
    import hook_common
    import redact_common
    return fleet_report, fleet_inject, hook_common, redact_common


def run_stop(report, monkeypatch, tmp_path: Path, text: str) -> list[dict]:
    """Run the Stop hook for a child whose last reply is *text*; the records
    appended to its parent's inbox (state and launch dir under *tmp_path*)."""
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "child.json").write_text(
        json.dumps({"session_id": SID, "name": CHILD, "parent": PARENT}), encoding="utf-8")
    reply = {"type": "assistant", "uuid": "turn-1",
             "message": {"content": [{"type": "text", "text": text}]}}
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(reply) + "\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FLEET_LAUNCH_DIR", str(launch))
    monkeypatch.delenv("FLEET_SESSION_NAME", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        json.dumps({"session_id": SID, "transcript_path": str(transcript)})))
    assert report.run_hook() == 0
    inbox = tmp_path / "state" / "inbox" / f"{PARENT}.jsonl"
    if not inbox.exists():
        return []
    return [json.loads(row) for row in inbox.read_text(encoding="utf-8").splitlines()]
