"""W12 operator waits (D-677) — what waits on the operator, by kind.

Loads ALL page scripts in render.py JS_FILES order (pattern of
tests/test_ui_real_page.py: the fake DOM + shell markup are reused verbatim,
only SNAP and main() differ) with a snapshot covering explicit `wait`,
fallback `wait`, cluster roll-up, transitive dependents, legend and Needs.

Mutations that fail these tests (one per behaviour):
- waitKind returning item.wait without the terminal guard (done item keeps a wait).
- waitKind fallback ignoring evidence_kinds (Q-9 falls to "task", phase stays "task").
- cluster roll-up taking the first kind instead of answer-wins (phase A reads "task").
- blockedVia stopping at direct deps only (D2 loses its transitive via).
- graph wait classes/tip deleted (nodes lose wait-answer/state-blocked/waitTip).
- legend array entries or Needs badge lines deleted (labels missing).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from plangraph.render import JS_FILES  # noqa: E402
import test_ui_real_page as rp  # noqa: E402

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

SNAP = {
    "items": [
        {"key": "projA:A1", "id": "A1", "project": "projA", "phase": "P1",
         "title": "Operator approval needed", "kind": "lane", "state": "open",
         "owner": "op", "operator": True, "wait": "answer", "priority": 1,
         "estimate_h": 2, "depends_on": [], "evidence": []},
        {"key": "projA:T1", "id": "T1", "project": "projA", "phase": "P1",
         "title": "Do the routine thing", "kind": "lane", "state": "open",
         "owner": "op", "operator": True, "priority": 1,
         "estimate_h": 2, "depends_on": [], "evidence": []},
        {"key": "projA:D1", "id": "D1", "project": "projA", "phase": "P1",
         "title": "Downstream work", "kind": "lane", "state": "open",
         "owner": "alice", "priority": 1,
         "estimate_h": 2, "depends_on": ["projA:A1"], "evidence": []},
        {"key": "projA:D2", "id": "D2", "project": "projA", "phase": "P1",
         "title": "Transitive downstream", "kind": "lane", "state": "open",
         "owner": "alice", "priority": 1,
         "estimate_h": 2, "depends_on": ["projA:D1"], "evidence": []},
        {"key": "projA:DX", "id": "DX", "project": "projA", "phase": "P1",
         "title": "Finished approval", "kind": "lane", "state": "done",
         "owner": "op", "operator": True, "wait": "answer",
         "estimate_h": 2, "depends_on": [], "evidence": []},
        {"key": "projB:Q-9", "id": "Q-9", "project": "projB", "phase": "P1",
         "title": "Q: which region?", "kind": "lane", "state": "open",
         "owner": "op", "operator": True, "priority": 1,
         "estimate_h": 1, "depends_on": [],
         "evidence": [{"kind": "decision", "ref": {"id": "D-1"},
                       "at": "2026-01-01T00:00:00Z"}]},
        {"key": "projC:C1", "id": "C1", "project": "projC", "phase": "P1",
         "title": "File the report", "kind": "lane", "state": "open",
         "owner": "op", "operator": True, "priority": 1,
         "estimate_h": 2, "depends_on": [], "evidence": []},
    ]
}

MY_MAIN = r"""
function main() {
  FILES.forEach(function (f) { require(WEB + "/" + f); });
  var PlanUI = global.PlanUI;
  var vm = PlanUI.app.vm;
  function rec(k) { return vm.items.get(k); }
  function phaseWait(pname, phname) {
    var ps = vm.projects.filter(function (p) { return p.name === pname; });
    if (!ps.length) return null;
    var fs = (ps[0].phases || []).filter(function (f) { return f.name === phname; });
    return fs.length ? (fs[0].wait || null) : null;
  }
  function projWait(pname) {
    var ps = vm.projects.filter(function (p) { return p.name === pname; });
    return ps.length ? (ps[0].wait || null) : null;
  }
  var els = PlanUI.buildElements(vm, {});
  function clsIn(elsx, id) {
    var ns = elsx.nodes.filter(function (n) { return n.data && n.data.id === id; });
    return ns.length ? ns[0].classes : null;
  }
  function tipOf(id) {
    var ns = els.nodes.filter(function (n) { return n.data && n.data.id === id; });
    return ns.length ? (ns[0].data.waitTip || null) : null;
  }
  var elsCol = PlanUI.buildElements(vm, { collapsedProjects: new Set(["projC"]) });
  var styles = PlanUI.graphStyleRules
    ? PlanUI.graphStyleRules({ cssVar: function () { return ""; } }) : [];
  emit({
    waitA1: rec("projA:A1").wait,
    waitT1: rec("projA:T1").wait,
    waitQ9: rec("projB:Q-9").wait,
    waitPlain: rec("projA:D1").wait,
    waitDone: rec("projA:DX").wait,
    blockedD1: rec("projA:D1").blockedOnOperator,
    blockedD2: rec("projA:D2").blockedOnOperator,
    blockedA1: rec("projA:A1").blockedOnOperator,
    phaseA: phaseWait("projA", "P1"),
    phaseB: phaseWait("projB", "P1"),
    projA: projWait("projA"),
    projC: projWait("projC"),
    clsA1: clsIn(els, "t:projA:A1"),
    clsT1: clsIn(els, "t:projA:T1"),
    clsD1: clsIn(els, "t:projA:D1"),
    tipD1: tipOf("t:projA:D1"),
    tipD2: tipOf("t:projA:D2"),
    clsPhaseA: clsIn(els, "ph:projA|P1"),
    clsProjA: clsIn(els, "p:projA"),
    clsProjCCol: clsIn(elsCol, "pc:projC"),
    legend: doc.getElementById("legend").textContent,
    needs: doc.getElementById("sb-panel-needs").textContent,
    styleSels: styles.map(function (s) { return s.selector; }),
    wkExplicit: PlanUI.waitKind({ operator: true, state: "open", wait: "task" }),
    wkFallbackTask: PlanUI.waitKind({ operator: true, state: "open", title: "Do it", evidence: [] }),
    wkFallbackAnswer: PlanUI.waitKind({ operator: true, state: "open", id: "Q-7", title: "Q: go?", evidence_kinds: ["decision"] }),
    wkNonOp: PlanUI.waitKind({ operator: false, state: "open", title: "x" })
  });
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    src = rp.HARNESS
    patched = re.sub(r"var SNAP = .*?\n\};", "var SNAP = %s;" % json.dumps(SNAP),
                     src, count=1, flags=re.DOTALL)
    assert patched != src
    head, _, _ = patched.partition("function main()")
    harness = tmp_path / "operator_wait_harness.js"
    harness.write_text(head + MY_MAIN, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), json.dumps(list(JS_FILES))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_wait_kind_explicit_and_fallback(tmp_path):
    # Mutation that fails it: waitKind ignoring evidence_kinds (Q-9 reads "task").
    out = run_node(tmp_path)
    assert out["wkExplicit"] == "task"
    assert out["wkFallbackTask"] == "task"
    assert out["wkFallbackAnswer"] == "answer"
    assert out["wkNonOp"] is None
    assert out["waitA1"] == "answer"      # explicit wait
    assert out["waitT1"] == "task"        # fallback: operator, open
    assert out["waitQ9"] == "answer"      # fallback: ["decision"] + Q-shape
    assert out["waitPlain"] is None       # not operator-owned
    assert out["waitDone"] is None        # terminal items never wait


@need_node
def test_cluster_roll_up_answer_wins(tmp_path):
    # Mutation that fails it: roll-up taking the first kind (phase A reads "task").
    out = run_node(tmp_path)
    assert out["phaseA"] == "answer"      # one answer + one task -> answer
    assert out["phaseB"] == "answer"
    assert out["projA"] == "answer"
    assert out["projC"] == "task"         # only task waits
    assert "wait-answer" in out["clsA1"]
    assert "wait-task" in out["clsT1"]
    assert "wait-answer" in out["clsPhaseA"]
    assert "wait-answer" in out["clsProjA"]
    assert "wait-task" in out["clsProjCCol"]


@need_node
def test_dependent_marked_blocked_on_operator(tmp_path):
    # Mutation that fails it: blockedVia stopping at direct deps (D2 loses via).
    out = run_node(tmp_path)
    assert out["blockedD1"] == {"kind": "answer", "via": "projA:A1"}
    assert out["blockedD2"] == {"kind": "answer", "via": "projA:A1"}
    assert out["blockedA1"] is None
    assert "state-blocked" in out["clsD1"]
    assert out["tipD1"] == "waits on operator (answer) via projA:A1"
    assert out["tipD2"] == "waits on operator (answer) via projA:A1"


@need_node
def test_legend_and_needs_badges(tmp_path):
    # Mutation that fails it: the legend array entries deleted (legend misses
    # both entries) or the Needs badge lines deleted (needs rows unbadged).
    out = run_node(tmp_path)
    assert "Needs your answer" in out["legend"]
    assert "Needs your task" in out["legend"]
    assert "Needs your answer" in out["needs"]
    assert "Needs your task" in out["needs"]
    assert "node.wait-answer" in out["styleSels"]
    assert "node.wait-task" in out["styleSels"]


def test_wait_css_tokens_light_and_dark():
    # Mutation that fails it: a single token or no dark variant.
    css = (WEB / "page_graph.css").read_text(encoding="utf-8")
    assert "--wait-answer" in css and "--wait-task" in css
    dark = css[css.index("@media"):]
    assert "--wait-answer" in dark and "--wait-task" in dark
