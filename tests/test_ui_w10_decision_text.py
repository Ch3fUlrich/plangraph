"""W10: Decisions page shows decision text (adapter.text + snapshot + page JS).

Mutations that fail these tests:
- adapter.text returning None / reading the wrong column.
- snapshot omitting decision_text (or adding top-level decisions/questions).
- deriveDecisions ignoring snap.decision_text (old "Decision D-xxx" title).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.evidence_decisions import DecisionsAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

TEXT548 = "Approve the new cache layer for demo"
TEXT549 = "Retire the legacy queue worker"


def _decisions_md(tmp_path, extra=""):
    p = tmp_path / "DECISIONS.md"
    p.write_text(
        "| ID | Note | Decision |\n|---|---|---|\n"
        f"| D-548 | scope | {TEXT548} |\n"
        f"| D-549 | scope | {TEXT549} |\n" + extra,
        encoding="utf-8",
    )
    return p


def test_adapter_text_returns_decision_cell(tmp_path):
    # Fails if text() reads the wrong column or skips whitespace collapsing.
    p = _decisions_md(tmp_path, "| D-560 | n |  Spaced    out\ttext |\n")
    adapter = DecisionsAdapter(p)
    assert adapter.text("D-548") == TEXT548
    assert adapter.text("d-548") == TEXT548
    assert adapter.text("D-0548") == TEXT548
    assert adapter.text("D-560") == "Spaced out text"
    assert adapter.text("D-999") is None
    assert DecisionsAdapter(None).text("D-548") is None


def test_snapshot_decision_text_only_referenced(tmp_path):
    # Fails if _snapshot omits decision_text or includes unreferenced ids.
    world = World(tmp_path)
    try:
        (key,) = seed(world, [("A", [])])
        world.store.ctx = SimpleNamespace(
            decisions=DecisionsAdapter(_decisions_md(tmp_path)))
        epoch = to_claimed(world, key)
        world.store.add_evidence(
            world.p("l1main"), key, epoch, "decision", {"id": "D-548"})
        snap = world.store.snapshot(world.p("reader"))
        assert snap["decision_text"] == {"D-548": TEXT548}
        assert "decisions" not in snap and "questions" not in snap
        assert all(len(v) <= 600 for v in snap["decision_text"].values())
    finally:
        world.close()


def test_snapshot_no_adapter_is_empty_dict(tmp_path):
    # Fails if _snapshot raises instead of emitting {} with no adapter.
    world = World(tmp_path)
    try:
        (key,) = seed(world, [("A", [])])
        epoch = to_claimed(world, key)
        world.store.add_evidence(
            world.p("l1main"), key, epoch, "decision", {"id": "D-548"})
        snap = world.store.snapshot(world.p("reader"))
        assert snap["decision_text"] == {}
    finally:
        world.close()


need_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")


@need_node
def test_ui_decisions_table_shows_text(tmp_path):
    # Fails if deriveDecisions ignores snap.decision_text (title stays
    # "Decision D-548" and the table never contains the sentence).
    import test_ui_real_page as rp

    snap = {
        "decision_text": {"D-548": TEXT548, "D-549": TEXT549},
        "items": [
            {"key": "projA:T1", "id": "T1", "project": "projA", "phase": "P1",
             "title": "Task one", "kind": "lane", "state": "open",
             "owner": "alice", "priority": 1, "estimate_h": 2,
             "depends_on": [],
             "evidence": [{"kind": "decision", "ref": {"id": "D-548"},
                           "at": "2026-01-01T00:00:00Z"}]},
            {"key": "projB:T2", "id": "T2", "project": "projB", "phase": "P1",
             "title": "Task two", "kind": "lane", "state": "open",
             "owner": "bob", "priority": 1, "estimate_h": 2,
             "depends_on": [],
             "evidence": [{"kind": "decision", "ref": {"id": "D-549"},
                           "at": "2026-01-02T00:00:00Z"}]},
            {"key": "projA:Q-1", "id": "Q-1", "project": "projA",
             "phase": "P1", "title": "Q: open question?", "kind": "lane",
             "state": "open", "owner": "alice", "priority": 5,
             "estimate_h": 1, "depends_on": [], "evidence": []},
        ],
    }
    src = rp.HARNESS
    patched = re.sub(r"var SNAP = .*?\n\};",
                     "var SNAP = %s;" % json.dumps(snap),
                     src, count=1, flags=re.DOTALL)
    assert patched != src
    harness = tmp_path / "w10_harness.js"
    harness.write_text(patched, encoding="utf-8")
    cmd = ["node", str(harness), str(rp.WEB), json.dumps(list(rp.JS_FILES))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    assert out["decTableFound"] is True
    assert TEXT548 in (out["decText"] or ""), out["decText"]
    assert TEXT549 in (out["decText"] or ""), out["decText"]
