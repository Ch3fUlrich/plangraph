"""SYNC-MD-IMPORT (page side): QUESTIONS rows and MODEL-KINDS questions are listed as
questions; an answered one shows the keeper's answer text and decision id."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

JS = r"""
"use strict";
require(process.argv[2]); var PlanUI = require(process.argv[3]);
function it(id, kw) {
  var o = { project: "routing", id: id, key: "routing:" + id, title: "t " + id, kind: "lane",
            track: "T", owner: "o", state: "open", estimate_h: 1, depends_on: [], operator: false,
            priority: 0, epoch: 0, evidence: [] };
  Object.keys(kw || {}).forEach(function (k) { o[k] = kw[k]; });
  return o;
}
var vm = PlanUI.buildViewModel({ items: [
  it("OS-1", { track: "QUESTIONS", wait: "answer", state: "done",
               answer: { text: "answered by D-7", decision_id: "D-7" } }),
  it("OS-2", { track: "QUESTIONS", wait: "task" }),
  it("PH-OPERATOR", { track: "QUESTIONS", kind: "gate" }),
  it("ASK", { kind: "question", wait: "answer" }),
  it("DEC", { kind: "decision", state: "done", answer: { text: "go", decision_id: "D-9" } }),
  it("PLAIN", {})
]});
process.stdout.write(JSON.stringify((vm.questions || []).map(function (q) {
  return [q.id, q.status, q.answer, q.decision || null]; })));
"""


@need_node
def test_questions_include_question_rows_and_answers(tmp_path):
    rp = tmp_path / "q.js"
    rp.write_text(JS, encoding="utf-8")
    proc = subprocess.run(["node", str(rp), str(WEB / "page.js"), str(WEB / "page_model.js")],
                          capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = {row[0]: row[1:] for row in json.loads(proc.stdout)}
    assert got == {"OS-1": ["answered", "answered by D-7", "D-7"],
                   "ASK": ["open", None, None],
                   "DEC": ["answered", "go", "D-9"]}
