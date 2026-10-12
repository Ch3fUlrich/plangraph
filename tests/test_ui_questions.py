"""Question-lane rule of the page adapter (page_model.js ``questionOf``):
a lane whose title starts "Q:" OR whose id matches ^Q- is a question lane
in ANY project, not only the first one (Router decision 2, project-agnostic
form).

Real-shape snapshot (items only, no questions/decisions keys), synthetic
names only; one shared node harness, skipped cleanly when node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
NOW = "2026-10-01T00:00:00Z"

# node harness, mode "vm": same shape as test_ui_pure.py (JSON on stdout).
HARNESS = r"""
"use strict";
var fs = require("fs");
require(process.argv[2]);                 // page.js -> global.PlanGraph
var PlanUI = require(process.argv[3]);    // page_model.js -> PlanUI
var snap = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
var vm = PlanUI.buildViewModel(snap);
var out = {
  projectNames: vm.projects.map(function (p) { return p.name; }),
  questions: vm.questions,
  needsYou: vm.needsYou.map(function (e) {
    return e.status !== undefined ? {t: "q", k: e.id} : {t: "item", k: e.key}; })
};
process.stdout.write(JSON.stringify(out));
"""


def snapshot() -> dict:
    """REAL-shaped snapshot: items only, no questions/decisions keys."""
    def item(proj, iid, title, track, **kw):
        base = {"project": proj, "id": iid, "key": f"{proj}:{iid}", "title": title,
                "kind": "lane", "track": track, "owner": "cinder-bot", "state": "open",
                "estimate_h": 2, "depends_on": [], "done_criteria": "Meets DONE.",
                "operator": False, "priority": 0, "evidence": [], "updated_at": NOW}
        base.update(kw)
        return base

    items = [
        item("atlas", "A-1", "Replay the atlas ingest log", "T0", owner="atlas-lead"),
        # ^-lane id in a project other than the first one -> question lane
        item("cinder", "Q-MISSING-TITLES", "Which fence rotates first?", "T1"),
        # non-Q lane with a non-"Q:" title in that same project -> NOT a question
        item("cinder", "T-1", "Replay the vault ledger", "T1", owner="atlas-lead"),
    ]
    return {"generated": NOW, "head": "synth-q001", "items": items}


def vm_out(tmp_path: Path) -> dict:
    p = tmp_path / "q_snapshot.json"
    p.write_text(json.dumps(snapshot()), encoding="utf-8")
    harness = tmp_path / "q_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB / "page.js"), str(WEB / "page_model.js"), str(p)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_q_lane_in_non_first_project_is_a_question_lane(tmp_path):
    out = vm_out(tmp_path)
    # cinder is a real project but not the first one
    assert out["projectNames"] == ["atlas", "cinder"]
    qby = {q["id"]: q for q in out["questions"]}
    assert set(qby) == {"Q-MISSING-TITLES"}          # exactly one question lane
    q = qby["Q-MISSING-TITLES"]
    assert q["project"] == "cinder"
    assert q["item"] == "cinder:Q-MISSING-TITLES"
    assert q["status"] == "open"                     # open -> surfaces in needsYou
    # the non-Q lane in the same project is NOT classified as a question
    assert "T-1" not in qby
    assert "cinder:T-1" not in {entry["item"] for entry in out["questions"]}


@need_node
def test_open_q_lane_appears_in_needs_you_but_closed_does_not(tmp_path):
    snap = snapshot()
    # close the non-questions: only the open question lane may rank in needsYou
    out = vm_out(tmp_path)
    assert out["needsYou"] == [{"t": "q", "k": "Q-MISSING-TITLES"}]
    assert "T-1" not in [entry["k"] for entry in out["needsYou"]]
    # guardian: snapshot really is items-only (derivation, not passthrough)
    assert snap.get("questions") is None and snap.get("decisions") is None
