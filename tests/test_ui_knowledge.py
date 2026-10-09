"""PG-KG-UI (D-879): Knowledge block on item detail + per-project Knowledge tab, data only from kg-export.json.

Full JS_FILES order on the fake DOM of tests/kg_ui_dom.js (same contract as tests/test_ui_activity.py).
Rules: hidden without/with disabled/garbage export, no errors; superseded marker + current node; dangling and
unavailable refs marked; reverse index node -> items; every export string through textContent (XSS) and never
innerHTML; prototype-pollution-safe; load() tolerates fetch failure.
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

from plangraph.render import JS_FILES  # noqa: E402

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

XSS = "<img src=x onerror=alert(1)>"

HEAD = r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FILES = JSON.parse(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }
console.log = function () {};
"""

SCEN = r"""
var innerHtmlWrites = 0;
["innerHTML", "outerHTML"].forEach(function (p) {
  Object.defineProperty(El.prototype, p, { set: function () { innerHtmlWrites++; }, get: function () { return ""; } });
});
El.prototype.insertAdjacentHTML = function () { innerHtmlWrites++; };

function item(proj, iid, title, refs) {
  return { key: proj + ":" + iid, id: iid, project: proj, phase: "P1", title: title, kind: "lane",
    state: "open", owner: "op", priority: 1, estimate_h: 2, depends_on: [], evidence: [], kg_refs: refs || [] };
}
var SNAP = { generated: "2026-10-09T10:00:00Z", items: [
  item("p1", "A", "Alpha", ["g:Decision:d-old", "g:Rule:r-1", "g:Decision:gone", "zz:Rule:r-9"]),
  item("p1", "B", "Beta", []),
  item("p2", "C", "Gamma", ["g:Decision:d-new", "g:Decision:d-xss"])
] };
function N(graph, type, slug, o) {
  var n = { graph: graph, type: type, slug: slug, title: slug, status: null, severity: null, kind: null,
    excerpt: null, project: null, superseded_by: null, current: graph + ":" + type + ":" + slug };
  Object.keys(o || {}).forEach(function (k) { n[k] = o[k]; });
  return n;
}
var EXPORT = {
  enabled: true, generated: "2026-10-09T10:00:00Z", graphs: ["g"], unresolved_graphs: ["zz"], truncated: false,
  nodes: {
    "g:Decision:d-old": N("g", "Decision", "d-old", { title: "Old way", status: "superseded", project: "p1",
      superseded_by: "g:Decision:d-new", current: "g:Decision:d-new", excerpt: "because old" }),
    "g:Decision:d-new": N("g", "Decision", "d-new", { title: "New way", status: "accepted", project: "p1" }),
    "g:Decision:d-xss": N("g", "Decision", "d-xss", { title: "%XSS%", status: "accepted", project: "p2", excerpt: "%XSS%" }),
    "g:Rule:r-1": N("g", "Rule", "r-1", { title: "Never do X", severity: "must", project: "p1" }),
    "g:Component:c-1": N("g", "Component", "c-1", { title: "Keeper", kind: "service", project: "p1" })
  },
  items: { "p1:A": ["g:Decision:d-old", "g:Rule:r-1", "g:Decision:gone", "zz:Rule:r-9"],
           "p2:C": ["g:Decision:d-new", "g:Decision:d-xss"] },
  reverse: { "g:Decision:d-old": ["p1:A"], "g:Decision:d-new": ["p1:A", "p2:C"], "g:Rule:r-1": ["p1:A"],
             "g:Decision:d-xss": ["p2:C"] },
  projects: { p1: { nodes: ["g:Decision:d-new", "g:Decision:d-old", "g:Rule:r-1", "g:Component:c-1"] },
              p2: { nodes: ["g:Decision:d-new", "g:Decision:d-xss"] } }
};
EXPORT = JSON.parse(JSON.stringify(EXPORT).split("%XSS%").join(JSON.stringify(%XSSJSON%).slice(1, -1)));

add(body, "header", "top");
["search", "search-toggle", "tab-graph", "tab-list", "tab-qa", "tab-needs", "tab-activity", "tab-progress",
 "tab-knowledge", "tab-settings", "sb-toggle", "sb-scrim", "sb-resize", "panel-graph", "panel-list", "list-content",
 "panel-qa", "sb-panel-qa", "sb-panel-answers", "panel-needs", "sb-panel-needs", "panel-activity",
 "sb-panel-activity", "panel-progress", "sb-panel-progress", "panel-knowledge",
 "panel-settings", "sb-panel-settings", "project-tree", "meta-line", "progress-bar", "progress-text", "legend",
 "cy", "graph-empty", "collapse-done", "inbox-badge", "sb-count-qa", "sb-count-needs", "sb-count-answers"
].forEach(function (id) { add(body, "div", "", [["id", id]]); });
add(doc.getElementById("panel-knowledge"), "div", "", [["id", "sb-panel-knowledge"]]);
doc.getElementById("tab-knowledge").hidden = true;   /* template: starts hidden */
doc.getElementById("panel-knowledge").hidden = true;
var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
add(sidebar, "div", "", [["id", "sb-panel-projects"]]);
var snapEl = add(body, "script", "", [["id", "snapshot-data"], ["type", "application/json"]]);
snapEl.textContent = JSON.stringify(SNAP);
var tabbar = add(body, "nav", "tabs", [["id", "tabbar"]]);
["graph", "table", "qa", "needs", "menu", "settings"].forEach(function (s) {
  add(tabbar, "button", "tab-btn", [["data-screen", s]]);
});
add(body, "aside", "details", [["id", "details"]]);

function all(root, pred) { return walk(root, []).filter(pred); }
function cls(n, c) { return (n.className || "").split(/\s+/).indexOf(c) >= 0; }
function byClass(root, c) { return all(root, function (n) { return n.nodeType === 1 && cls(n, c); }); }
function fire(n, type, extra) { (n.listeners[type] || []).slice().forEach(function (f) { f(Object.assign({ type: type, target: n }, extra || {})); }); }
function detailsText() { return doc.getElementById("details").textContent; }
function hasTag(root, tag) { return all(root, function (n) { return n.tagName === tag; }).length; }

function main() {
  FILES.forEach(function (f) { require(WEB + "/" + f); });
  var PlanUI = global.PlanUI, K = PlanUI.Knowledge, out = { files: FILES.length };
  out.hasApi = !!(K && K.setData && K.block && K.load && PlanUI.KnowledgePage && PlanUI.KnowledgePage.render);
  var tab = doc.getElementById("tab-knowledge"), panel = doc.getElementById("panel-knowledge");

  /* 1. no export yet */
  PlanPage.selectNode("p1:A");
  out.noData = { tabHidden: tab.hidden, block: K.block("p1:A"), detailsHasBlock: byClass(doc.getElementById("details"), "kg-block").length };

  /* 2. disabled / garbage exports never throw and keep everything hidden */
  var bad = [null, undefined, "x", 7, [], {}, { enabled: false }, { enabled: true }, { enabled: true, nodes: "x" },
             { enabled: true, nodes: [], items: [] }, JSON.parse('{"enabled":true,"nodes":{"__proto__":{"polluted":1}}}')];
  out.bad = bad.map(function (b) {
    try { K.setData(b); return { tabHidden: tab.hidden, block: K.block("p1:A") === null }; }
    catch (e) { return { error: String(e) }; }
  });
  out.polluted = ({}).polluted === undefined;

  /* 3. real export: item detail block */
  K.setData(EXPORT);
  out.tabHiddenAfter = tab.hidden;
  PlanPage.selectNode("p1:B");
  out.noRefsItem = byClass(doc.getElementById("details"), "kg-block").length;
  PlanPage.selectNode("p1:A");
  var d = doc.getElementById("details");
  out.blockCount = byClass(d, "kg-block").length;
  out.itemText = detailsText();
  out.superseded = byClass(d, "kg-superseded").map(function (n) { return n.textContent; });
  out.dangling = byClass(d, "kg-dangling").map(function (n) { return n.textContent; });
  out.unavailable = byClass(d, "kg-unavailable").map(function (n) { return n.textContent; });
  out.rows = byClass(d, "kg-node").length;

  /* 4. XSS: item p2:C references the hostile node */
  PlanPage.selectNode("p2:C");
  out.xssText = detailsText();
  out.xssImg = hasTag(doc.body, "IMG") + hasTag(doc.body, "SCRIPT") - 1; /* -1: the snapshot <script> */

  /* 5. tab */
  PlanUI.Pages.show("knowledge");
  out.tabPanelHidden = panel.hidden;
  out.tabSelected = tab.getAttribute("aria-selected");
  var sel = all(panel, function (n) { return n.tagName === "SELECT"; })[0];
  out.optionValues = sel ? all(sel, function (n) { return n.tagName === "OPTION"; }).map(function (o) { return o.getAttribute("value"); }) : null;
  out.p1Text = panel.textContent;
  out.p1Rows = byClass(panel, "kg-node").length;
  var revBtn = all(panel, function (n) { return n.tagName === "BUTTON" && cls(n, "keylink"); });
  out.revKeys = revBtn.map(function (b) { return b.textContent; });
  if (sel) { sel.value = "p2"; fire(sel, "change"); }
  out.p2Text = panel.textContent;
  out.xssTabImg = hasTag(panel, "IMG");
  var k = all(panel, function (n) { return n.tagName === "BUTTON" && cls(n, "keylink"); })[0];
  out.firstTabKey = k ? k.textContent : null;
  if (k) k.click();
  out.selectedFromTab = PlanPage.state.selected;
  out.noInnerHtml = innerHtmlWrites;

  /* 6. disabling while on the tab falls back to the graph */
  PlanUI.Pages.show("knowledge");
  K.setData({ enabled: false });
  out.afterDisable = { tabHidden: tab.hidden, page: PlanUI.Pages.current(), panelHidden: panel.hidden };

  /* 7. load() via XMLHttpRequest */
  var seq = [], urls = [], mode = "404";
  global.XMLHttpRequest = function () {
    var self = this;
    this.open = function (m, u) { urls.push(m + " " + u); };
    this.send = function () {
      Promise.resolve().then(function () {
        if (mode === "net") return self.onerror();
        self.status = mode === "404" ? 404 : 200;
        self.responseText = mode === "badjson" ? "{nope" : JSON.stringify(EXPORT);
        self.onload();
      });
    };
  };
  function step(name, next) { mode = name; return K.load().then(function () { seq.push([name, tab.hidden]); return next && next(); }); }
  step("404", function () { return step("net", function () { return step("badjson", function () { return step("ok", function () {
    delete global.XMLHttpRequest;
    return K.load();
  }); }); }); })
   .then(function () { out.loadSeq = seq; out.fetchUrl = urls[0]; emit(out); })
   .catch(function (e) { emit({ error: String((e && e.stack) || e) }); });
}
try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def _harness() -> str:
    dom = (Path(__file__).resolve().parent / "kg_ui_dom.js").read_text(encoding="utf-8")
    return HEAD + dom + SCEN.replace("%XSSJSON%", json.dumps(XSS))


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kgui")
    h = tmp / "h.js"
    h.write_text(_harness(), encoding="utf-8")
    proc = subprocess.run(["node", str(h), str(WEB), json.dumps(list(JS_FILES))],
                          capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert "error" not in res, res.get("error")
    return res


def test_static_wiring_and_no_html_sinks():
    assert "page_kg.js" in JS_FILES and "page_kg_tab.js" in JS_FILES
    assert JS_FILES.index("page_kg.js") < JS_FILES.index("page_boot.js")
    for name in ("page_kg.js", "page_kg_tab.js"):
        src = (WEB / name).read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", src), name
        assert len(src.splitlines()) <= 400
    tpl = (WEB / "page_template.html").read_text(encoding="utf-8")
    for needle in ('id="tab-knowledge"', 'id="panel-knowledge"', 'id="sb-panel-knowledge"'):
        assert needle in tpl
    assert re.search(r'id="tab-knowledge"[^>]*\bhidden\b', tpl), "tab starts hidden"


@need_node
def test_api_registered(out):
    assert out["hasApi"] is True


@need_node
def test_hidden_without_export(out):
    assert out["noData"] == {"tabHidden": True, "block": None, "detailsHasBlock": 0}


@need_node
def test_disabled_or_garbage_export_hidden_no_errors(out):
    assert out["bad"] == [{"tabHidden": True, "block": True}] * len(out["bad"])
    assert out["polluted"] is True


@need_node
def test_item_block_shows_resolved_nodes(out):
    assert out["tabHiddenAfter"] is False
    assert out["noRefsItem"] == 0
    assert out["blockCount"] == 1
    text = out["itemText"]
    for needle in ("Old way", "New way", "Never do X", "must", "superseded"):
        assert needle in text, needle
    assert out["rows"] == 4  # d-old, r-1, gone, zz


@need_node
def test_superseded_marker_names_current(out):
    assert len(out["superseded"]) == 1
    assert "New way" in out["superseded"][0] and "accepted" in out["superseded"][0]


@need_node
def test_dangling_and_unavailable_marked(out):
    assert len(out["dangling"]) == 1 and "gone" in out["dangling"][0]
    assert len(out["unavailable"]) == 1 and "zz:Rule:r-9" in out["unavailable"][0]


@need_node
def test_xss_in_item_block_is_text(out):
    assert XSS in out["xssText"]
    assert out["xssImg"] == 0


@need_node
def test_knowledge_tab_per_project_with_reverse_index(out):
    assert out["tabPanelHidden"] is False and out["tabSelected"] == "true"
    assert out["optionValues"] == ["p1", "p2"]
    assert out["p1Rows"] == 4
    for needle in ("Old way", "New way", "Never do X", "Keeper"):
        assert needle in out["p1Text"], needle
    assert {"p1:A", "p2:C"} <= set(out["revKeys"])  # d-new is referenced directly by C and via d-old by A
    assert "Never do X" in out["p1Text"] and "Old way" in out["p1Text"]


@need_node
def test_tab_project_switch_and_xss(out):
    assert XSS in out["p2Text"]
    assert out["xssTabImg"] == 0
    assert "Never do X" not in out["p2Text"]


@need_node
def test_tab_item_link_selects_item(out):
    assert out["firstTabKey"] == "p2:C"
    assert out["selectedFromTab"] == "p2:C"


@need_node
def test_never_writes_html(out):
    assert out["noInnerHtml"] == 0


@need_node
def test_disable_while_open_falls_back(out):
    assert out["afterDisable"]["tabHidden"] is True
    assert out["afterDisable"]["page"] == "graph"
    assert out["afterDisable"]["panelHidden"] is True


@need_node
def test_load_tolerates_failures_and_reads_sibling_file(out):
    assert out["loadSeq"] == [["404", True], ["net", True], ["badjson", True], ["ok", False]]
    assert out["fetchUrl"] == "GET kg-export.json"
