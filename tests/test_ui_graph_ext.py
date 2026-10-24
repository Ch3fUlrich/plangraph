"""Tests for PlanUI Graph View Extension Registry, Styles, and Jump fix (GU-G2b0)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
import pytest
from plangraph import render

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def run_node_snippet(tmp_path: Path, script_body: str):
    runner = tmp_path / "test_runner.js"
    full_script = f"""
"use strict";
require({json.dumps(str(WEB / "page.js"))});
require({json.dumps(str(WEB / "page_graph_viewport.js"))});
require({json.dumps(str(WEB / "page_graph_ext.js"))});

{script_body}
"""
    runner.write_text(full_script, encoding="utf-8")
    proc = subprocess.run(["node", str(runner)], capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, f"Node script failed:\n{proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else None


@need_node
def test_t1_registry(tmp_path: Path):
    """T1: register order = call order; replacing keeps position; unregisterAll empties; list() returns ids."""
    res = run_node_snippet(tmp_path, r"""
var GraphExt = PlanUI.GraphExt;
GraphExt.unregisterAll();
GraphExt.register({ id: "first" });
GraphExt.register({ id: "second" });
GraphExt.register({ id: "third" });
var list1 = GraphExt.list();
GraphExt.register({ id: "second", updated: true });
var list2 = GraphExt.list();
GraphExt.unregisterAll();
process.stdout.write(JSON.stringify({ list1: list1, list2: list2, list3: GraphExt.list() }));
""")
    assert res["list1"] == ["first", "second", "third"]
    assert res["list2"] == ["first", "second", "third"]
    assert res["list3"] == []


@need_node
def test_t2_call_semantics(tmp_path: Path):
    """T2: styles concatenation; state merge (later wins) and null when none; transformElements chain; init/afterSync/bind."""
    res = run_node_snippet(tmp_path, r"""
var GraphExt = PlanUI.GraphExt;
GraphExt.unregisterAll();
GraphExt.register({ id: "s1", styles: function () { return [{ selector: "node.s1", style: {} }]; } });
GraphExt.register({ id: "s2", styles: function () { return [{ selector: "node.s2", style: {} }]; } });
var styles = GraphExt.call("styles", {});

GraphExt.unregisterAll();
GraphExt.register({ id: "st1", state: function () { return { a: 1, b: 2 }; } });
GraphExt.register({ id: "st2", state: function () { return { b: 99, c: 3 }; } });
var stateMerged = GraphExt.call("state", null, {});
GraphExt.unregisterAll();
var stateEmpty = GraphExt.call("state", null, {});

GraphExt.register({ id: "t1", transformElements: function (els) { return { nodes: els.nodes.concat(["n1"]), edges: els.edges }; } });
GraphExt.register({ id: "t2", transformElements: function () { return undefined; } });
GraphExt.register({ id: "t3", transformElements: function (els) { return { nodes: els.nodes.concat(["n2"]), edges: els.edges }; } });
var trans = GraphExt.call("transformElements", { nodes: [], edges: [] }, {});

GraphExt.unregisterAll();
var calls = [], ctx = { testKey: "ctxVal" };
GraphExt.register({
  id: "lifecycle",
  init: function (c) { calls.push({ hook: "init", val: c.testKey }); },
  afterSync: function (c) { calls.push({ hook: "afterSync", val: c.testKey }); },
  bind: function (c) { calls.push({ hook: "bind", val: c.testKey }); }
});
var rInit = GraphExt.call("init", null, ctx), rAfter = GraphExt.call("afterSync", null, ctx), rBind = GraphExt.call("bind", null, ctx);

process.stdout.write(JSON.stringify({
  stylesCount: styles.length, stylesSelectors: styles.map(function (s) { return s.selector; }),
  stateMerged: stateMerged, stateEmpty: stateEmpty, transNodes: trans.nodes,
  isInitUndef: rInit === undefined, isAfterUndef: rAfter === undefined, isBindUndef: rBind === undefined,
  lifecycleCalls: calls
}));
""")
    assert res["stylesCount"] == 2 and res["stylesSelectors"] == ["node.s1", "node.s2"]
    assert res["stateMerged"] == {"a": 1, "b": 99, "c": 3} and res["stateEmpty"] is None
    assert res["transNodes"] == ["n1", "n2"]
    assert res["isInitUndef"] is True and res["isAfterUndef"] is True and res["isBindUndef"] is True
    assert res["lifecycleCalls"] == [
        {"hook": "init", "val": "ctxVal"}, {"hook": "afterSync", "val": "ctxVal"}, {"hook": "bind", "val": "ctxVal"}
    ]


@need_node
def test_t3_error_isolation(tmp_path: Path):
    """T3: throwing ext does not stop others, errors gets {ext, hook, message}, cap of 20, throwing field gives null."""
    res = run_node_snippet(tmp_path, r"""
var GraphExt = PlanUI.GraphExt;
GraphExt.unregisterAll();
GraphExt.register({ id: "bad1", styles: function () { throw new Error("boom1"); } });
GraphExt.register({ id: "good1", styles: function () { return [{ selector: "node.good", style: {} }]; } });
var styles = GraphExt.call("styles", {});

GraphExt.register({ id: "extWithFields", fields: { badField: function () { throw new Error("bad-field-err"); }, goodField: function () { return "ok"; } } });
var f = GraphExt.fields({});

for (var i = 0; i < 30; i++) {
  GraphExt.register({ id: "thrower_" + i, init: function () { throw new Error("err_" + i); } });
  GraphExt.call("init", null, {});
}
process.stdout.write(JSON.stringify({
  stylesSelectors: styles.map(function (s) { return s.selector; }),
  fieldBad: f.badField, fieldGood: f.goodField,
  errorCount: GraphExt.errors.length, firstError: GraphExt.errors[0]
}));
""")
    assert res["stylesSelectors"] == ["node.good"]
    assert res["fieldBad"] is None and res["fieldGood"] == "ok"
    assert res["errorCount"] == 20
    assert res["firstError"]["ext"] == "bad1" and res["firstError"]["hook"] == "styles"
    assert "boom1" in res["firstError"]["message"]


@need_node
def test_t4_graph_style_rules(tmp_path: Path):
    """T4: graphStyleRules with fake env equals literal 29 selectors (incl. the data-guarded width/height rules), ext rules come last, phase-collapsed properties."""
    EXPECTED_SELECTORS = [
        "node", "node[width][height]", "node.task-node", "node.critical-node", "node.loop-node", "node.more-node", "node.exit-node",
        "node.exit-node.hollow", "node.exit-node.filled", "node.ghost-node, node.ghost-stub",
        "node.gate-node", "node.goal-node", ":parent", "node.project-compound",
        "node.phase-compound", "node.phase-collapsed, node.project-collapsed",
        "node.project-collapsed", "node.phase-collapsed", "node:selected", "edge", "edge[width]",
        "edge.critical-edge", ".dim", "node.state-ready", "node.state-blocked",
        "node.state-claimed", "node.state-done", "node.state-open",
        "node.state-in_review", "node.state-cancelled"
    ]
    res = run_node_snippet(tmp_path, r"""
PlanUI.GraphExt.unregisterAll();
var env = { cssVar: function (n) { return n; }, isPhone: function () { return false; } };
var rules = PlanUI.graphStyleRules(env);
PlanUI.GraphExt.register({ id: "testStylesExt", styles: function () { return [{ selector: "node.extra-rule", style: { width: 50 } }]; } });
var rulesWithExt = PlanUI.graphStyleRules(env);

var collapsedRule = null;
for (var i = 0; i < rules.length; i++) {
  if (rules[i].selector === "node.phase-collapsed, node.project-collapsed") { collapsedRule = rules[i]; break; }
}
var stateBorders = {};
["ready", "blocked", "claimed", "done", "open", "in_review", "cancelled"].forEach(function (st) {
  for (var j = 0; j < rules.length; j++) {
    if (rules[j].selector === "node.state-" + st) stateBorders[st] = rules[j].style["border-style"];
  }
});
process.stdout.write(JSON.stringify({
  selectors: rules.map(function (r) { return r.selector; }),
  lastSelectorWithExt: rulesWithExt[rulesWithExt.length - 1].selector,
  collapsedShape: collapsedRule.style.shape, collapsedWidth: collapsedRule.style.width,
  collapsedHeight: collapsedRule.style.height, collapsedTextWrap: collapsedRule.style["text-wrap"],
  collapsedTextMaxWidth: collapsedRule.style["text-max-width"],
  collapsedLabelIsFn: typeof collapsedRule.style.label === "function",
  stateBorders: stateBorders
}));
""")
    assert res["selectors"] == EXPECTED_SELECTORS
    assert res["lastSelectorWithExt"] == "node.extra-rule"
    assert res["collapsedShape"] == "round-rectangle"
    assert res["collapsedWidth"] == 300 and res["collapsedHeight"] == 72
    assert res["collapsedTextWrap"] == "wrap" and res["collapsedTextMaxWidth"] == 10000
    assert res["collapsedLabelIsFn"] is True
    for st in ["ready", "blocked", "claimed", "done", "open"]:
        assert res["stateBorders"][st] == "solid"
    for st in ["in_review", "cancelled"]:
        assert res["stateBorders"][st] == "dashed"


def test_t5_files_and_extension_points():
    """T5: File line limits, no innerHTML, JS_FILES order, and 5 extension point calls in view."""
    view_text = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    ext_text = (WEB / "page_graph_ext.js").read_text(encoding="utf-8")

    assert len(view_text.splitlines()) <= 388, f"page_graph_view.js <= 388 lines ({len(view_text.splitlines())})"
    assert len(ext_text.splitlines()) <= 300, f"page_graph_ext.js <= 300 lines ({len(ext_text.splitlines())})"
    assert "innerHTML" not in ext_text, "page_graph_ext.js must not contain innerHTML"

    assert "page_graph_ext.js" in render.JS_FILES and render.JS_FILES.count("page_graph_ext.js") == 1
    idx_graph = render.JS_FILES.index("page_graph.js")
    idx_ext = render.JS_FILES.index("page_graph_ext.js")
    assert idx_ext == idx_graph + 1, "page_graph_ext.js must directly follow page_graph.js"

    for hook in ["state", "init", "transformElements", "afterSync", "bind"]:
        assert view_text.count(f'GraphExt.call("{hook}"') == 1, f'Expected 1 occurrence of GraphExt.call("{hook}"'


@need_node
def test_t6_jump_after_dock(tmp_path: Path):
    """T6: Jump after dock measurements, freshCanvasSize helper, and static source check."""
    res = run_node_snippet(tmp_path, r"""
var resizeCount = 0;
var fakeCy = {
  _resized: false,
  resize: function () { resizeCount++; this._resized = true; },
  width: function () { return this._resized ? 600 : 980; },
  height: function () { return 680; }
};
var freshSize = PlanUI.freshCanvasSize(fakeCy);
var b = { x: 1559, y: 108, w: 240, h: 64 }, zoom = 0.9, inset = { top: 0, bottom: 0, left: 0, right: 0 };
var freshPan = PlanUI.panZoomFor(b, freshSize, zoom, inset);
var stalePan = PlanUI.panZoomFor(b, { w: 980, h: 680 }, zoom, inset);

var nullSize = PlanUI.freshCanvasSize(null);
var noResizeSize = PlanUI.freshCanvasSize({ width: function () { return 500; }, height: function () { return 300; } });

var unchangedCy = { resize: function () {}, width: function () { return 980; }, height: function () { return 680; } };
var unchangedSize = PlanUI.freshCanvasSize(unchangedCy);
var unchangedPan = PlanUI.panZoomFor(b, unchangedSize, zoom, inset);

process.stdout.write(JSON.stringify({
  freshSize: freshSize, resizeCount: resizeCount, freshPan: freshPan, stalePan: stalePan,
  nullSize: nullSize, noResizeSize: noResizeSize, unchangedSize: unchangedSize, unchangedPan: unchangedPan
}));
""")
    b = {"x": 1559, "y": 108, "w": 240, "h": 64}
    zoom = 0.9

    # (a) freshCanvasSize returns {w: 600, h: 680} and calls resize once
    assert res["freshSize"] == {"w": 600, "h": 680} and res["resizeCount"] == 1

    # Fresh size keeps target box inside [0, 600] on x
    fresh_pan_x = res["freshPan"]["pan"]["x"]
    assert b["x"] * zoom + fresh_pan_x >= 0
    assert (b["x"] + b["w"]) * zoom + fresh_pan_x <= 600

    # Stale pan centers at 490 instead of 300: with browser-measured rendered node right edge (half-width 110.7)
    # reaches 490 + 110.7 = 600.7 > 600 (0.7 px overshoot, selectedInsideCanvasX false).
    stale_pan_x = res["stalePan"]["pan"]["x"]
    stale_rendered_x2 = (b["x"] + b["w"] / 2) * zoom + stale_pan_x + 110.7
    fresh_rendered_x2 = (b["x"] + b["w"] / 2) * zoom + fresh_pan_x + 110.7
    assert stale_rendered_x2 > 600, f"Stale size placed rendered box outside canvas: {stale_rendered_x2} > 600"
    assert fresh_rendered_x2 <= 600, f"Fresh size placed rendered box inside canvas: {fresh_rendered_x2} <= 600"

    # (b) cy without resize and null cy do not throw
    assert res["nullSize"] == {"w": 0, "h": 0} and res["noResizeSize"] == {"w": 500, "h": 300}

    # (c) unchanged cy gives same pan as stale call
    assert res["unchangedSize"] == {"w": 980, "h": 680}
    assert abs(res["unchangedPan"]["pan"]["x"] - (-1021.1)) < 1e-6
    assert abs(res["unchangedPan"]["pan"]["y"] - 214) < 1e-6

    # (d) static source check of page_graph_view.js inside focusNode
    view_text = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    focus_fn_text = view_text.split("function focusNode")[1].split("function bindEvents")[0]
    assert focus_fn_text.count("PlanUI.freshCanvasSize(cy)") == 2
    assert "cy.width()" not in focus_fn_text and "cy.height()" not in focus_fn_text
