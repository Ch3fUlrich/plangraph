"""T2b shell probes — node-run tests for PlanUI.Sidebar / DetailPanel + statics.

Cycle 1 of T2b fixes the shell defects D1–D7; this file exercises the two DOM
components against a MINIMAL fake DOM written entirely inside the harness (no
jsdom, no packages): enough of createElement/appendChild/removeChild/
textContent/setAttribute/getAttribute/classList/addEventListener/hidden/query-
SelectorAll for page_side.js and page_details.js to mount and render.

Three checks, mirroring the D-list:
  D4    `PlanUI.Sidebar.mount(ui, vm)` builds #decisions-table and #qa-table;
  D6    `PlanUI.DetailPanel.mount(ui, vm)` followed by `ui.set("sel", key)`
        renders a REAL fixture item's title (not "Not in this snapshot."),
        while an unknown key still falls back to that message;
  D2/D5b/D7 statics: the template carries NO `<link>` tag (no favicon, so the
        self-containment guard stays as written) plus `id="sb-close"`,
        and page_boot.js wires `store: ui`.

Synthetic data only: the UI fixture (254 items), never a live snapshot.
Skips cleanly when node is missing (same contract as tests/test_ui_pure.py).
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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "synthetic_snapshot.json"

sys.path.insert(0, str(SRC))

SNAP = json.loads(FIXTURE.read_text(encoding="utf-8"))
# Real fixture keys (§ never invent a key the snapshot lacks).
KNOWN_KEY = SNAP["items"][0]["key"]
KNOWN_TITLE = SNAP["items"][0]["title"]
UNKNOWN_KEY = "ghost:NOT-IN-THIS-SNAPSHOT"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# Shared fake DOM + mount harness; modes "sidebar" | "details"; JSON on stdout.
HARNESS = r"""
"use strict";
var fs = require("fs");
var mode = process.argv[2];
var WEB = process.argv[3];
var FIXTURE = process.argv[4];
var KNOWN = process.argv[5];
var UNKNOWN = process.argv[6];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

/* ---- minimal fake DOM ---------------------------------------------------- */
var ids = {};

function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.attributes = {};
  this.listeners = {};
  this.style = {};
  this.className = "";
  this.hidden = false;
}
Object.defineProperty(El.prototype, "firstChild", {
  get: function () { return this.childNodes.length ? this.childNodes[0] : null; }
});
Object.defineProperty(El.prototype, "textContent", {
  get: function () {
    var t = this._text || "";
    for (var i = 0; i < this.childNodes.length; i++) t += this.childNodes[i].textContent || "";
    return t;
  },
  set: function (v) { this._text = String(v); this.childNodes = []; }
});
El.prototype.appendChild = function (c) {
  c.parentNode = this; this.childNodes.push(c); return c;
};
El.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) { this.childNodes.splice(i, 1); c.parentNode = null; }
  return c;
};
El.prototype.setAttribute = function (n, v) {
  this.attributes[n] = String(v);
  if (n === "id") ids[String(v)] = this;
  if (n === "class") this.className = String(v);
};
El.prototype.getAttribute = function (n) {
  return (n in this.attributes) ? this.attributes[n] : null;
};
El.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.dispatch = function (t) {          // only used for assertions
  (this.listeners[t] || []).forEach(function (f) { f({type: t}); });
};
El.prototype.closest = function () { return null; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.classList = {
  add: function (c) {},
  remove: function (c) {},
  toggle: function () {},
  contains: function (c) { return false; }
};
Object.defineProperty(El.prototype, "id", {
  get: function () { return this.attributes.id || ""; },
  set: function (v) { this.setAttribute("id", String(v)); }
});

/* `.a.b[attr='x y'] tag` — attribute values may contain a space. */
function tokenize(sel) {
  var out = [], i = 0, buf = "";
  while (i < sel.length) {
    var c = sel.charAt(i);
    if (c === "[") { var j = sel.indexOf("]", i); buf += sel.slice(i, j + 1); i = j + 1; }
    else if (c === " ") { if (buf) { out.push(buf); buf = ""; } i++; }
    else { buf += c; i++; }
  }
  if (buf) out.push(buf);
  return out;
}
function matches(node, part) {
  if (!node || node.nodeType !== 1) return false;
  var re = /\[([\w-]+)=['"]([^'"]*)['"]\]/g, m, rest = part;
  while ((m = re.exec(part))) {
    if (node.getAttribute(m[1]) !== m[2]) return false;
    rest = rest.replace(m[0], "");
  }
  var cls = rest.match(/\.[\w-]+/g) || [];
  var tag = rest.replace(/\.[\w-]+/g, "");
  if (tag && tag !== "*") {
    if (node.tagName !== tag.toUpperCase()
        && node.tagName.toLowerCase() !== String(tag).toLowerCase()) return false;
  }
  var own = (node.className || "").split(/\s+/);
  for (var i = 0; i < cls.length; i++) {
    if (own.indexOf(cls[i].slice(1)) < 0) return false;
  }
  return true;
}
function walk(node, out) {
  node.childNodes.forEach(function (c) { out.push(c); walk(c, out); });
  return out;
}
function ancestorOk(node, parts, k) {                 // parts[0..k-1] match above
  for (var a = node.parentNode; k >= 0; k--, a = a.parentNode) {
    while (a && !matches(a, parts[k])) a = a.parentNode;
    if (!a) return false;
  }
  return true;
}
function qsa(root, sel) {
  var parts = tokenize(sel), last = parts[parts.length - 1], hits = [];
  walk(root, []).forEach(function (n) {
    if (last && matches(n, last) && ancestorOk(n, parts, parts.length - 2)) hits.push(n);
  });
  return hits;
}

var body = new El("body");
var doc = {
  nodeType: 9,
  readyState: "loading",      /* init() waits for DOMContentLoaded; mount() drives it */
  body: body,
  createElement: function (t) { return new El(t); },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) { return qsa(body, sel); },
  querySelector: function (sel) { return qsa(body, sel)[0] || null; },
  addEventListener: function () {},
  removeEventListener: function () {}
};

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

/* page_template.html's shell, hand-built (fake DOM: no parser). */
var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
var tablist = add(sidebar, "div", "sb-tabs");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  var b = add(tablist, "button", "sb-tab", [["data-tab", t], ["role", "tab"]]);
  var c = add(b, "span", "sb-count", [["id", "sb-count-" + t], ["hidden", ""]]);
  c.hidden = true;
});
var sbBody = add(sidebar, "div", "sb-body");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  add(sbBody, "section", "sb-panel", [["id", "sb-panel-" + t], ["data-panel", t]]);
});
var tabbar = add(body, "nav", "tabs", [["id", "tabbar"]]);
add(tabbar, "span", "tab-badge", [["id", "inbox-badge"]]);
var details = add(body, "aside", "details bottom-sheet", [["id", "details"], ["data-snap", "peek"]]);
add(body, "div", "", [["id", "list-content"]]);

global.document = doc;

/* ---- PlanPage stub: page_details.js only needs these (page_ui.js is the
   real one in the browser; here the panel must be runnable on its own). ---- */
var PlanPage = {
  state: {selected: null, items: [], unblocks: {}, booted: true, cy: null},
  $: function (id) { return doc.getElementById(id); },
  el: function (tag, cls, text) {
    var n = new El(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  },
  clear: function (n) { while (n.firstChild) n.removeChild(n.firstChild); },
  selectNode: function (k) { this.state.selected = k; },
  focusNode: function (k) { this.state.selected = k; },
  visibleItems: function () { return []; },
  setTab: function () {},
  renderGraph: function () {}
};
global.PlanPage = PlanPage;

function main() {
  require(WEB + "/page.js");                 // PlanGraph
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_search.js");
  require(WEB + "/page_side.js");            // PlanUI.Sidebar (D4 table ids)
  require(WEB + "/page_side_tables.js");     // PlanUI.SideTables (sidebar tables)
  require(WEB + "/page_details.js");         // PlanUI.DetailPanel (D6 mount)

  var ui = new PlanUI.Store({});
  var vm = PlanUI.buildViewModel(JSON.parse(fs.readFileSync(FIXTURE, "utf8")));

  if (mode === "sidebar") {
    PlanUI.Sidebar.mount(ui, vm);
    function info(key) {
      var n = doc.getElementById(key);
      if (!n) return null;
      var inQa = false;
      for (var p = n.parentNode; p; p = p.parentNode) inQa = inQa || (p.id === "sb-panel-qa");
      return {tag: n.tagName, inQa: inQa, parent: n.parentNode ? n.parentNode.tagName : null};
    }
    emit({mode: mode,
          decisions: info("decisions-table"),
          qa: info("qa-table"),
          panels: [
            doc.getElementById("sb-panel-needs") ? 1 : 0,
            doc.getElementById("sb-panel-qa") ? 1 : 0
          ]});
    return;
  }

  /* mode === "details": D6 mount(ui, vm) + ui.set("sel", …) */
  PlanUI.DetailPanel.mount(ui, vm);
  var hiddenBefore = doc.getElementById("details").hidden;
  ui.set("sel", KNOWN);
  var knownText = doc.getElementById("details").textContent;
  var shown = doc.getElementById("details").hidden;
  ui.set("sel", UNKNOWN);
  var unknownText = doc.getElementById("details").textContent;
  emit({mode: mode, hiddenBefore: hiddenBefore, shownAfter: shown,
        title: KNOWN, knownText: knownText.slice(0, 4000),
        unknownText: unknownText.slice(0, 4000)});
}

try { main(); } catch (e) { emit({error: String((e && e.stack) || e)}); }
"""


def write_harness(tmp_path: Path) -> Path:
    p = tmp_path / "harness.js"
    p.write_text(HARNESS, encoding="utf-8")
    return p


def run_node(tmp_path: Path, mode: str) -> dict:
    cmd = ["node", str(write_harness(tmp_path)), mode, str(WEB), str(FIXTURE),
           KNOWN_KEY, UNKNOWN_KEY]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


# -- D4: Sidebar.mount(ui, vm) draws both sortable tables with stable ids ---- #

@need_node
def test_sidebar_mount_builds_decisions_and_qa_tables(tmp_path):
    out = run_node(tmp_path, "sidebar")
    assert out["decisions"] is not None, "decisions-table missing"
    assert out["qa"] is not None, "qa-table missing"
    assert out["decisions"]["tag"] == "TABLE"
    assert out["qa"]["tag"] == "TABLE"
    assert out["qa"]["inQa"] is True          # rendered into #sb-panel-qa
    assert out["panels"] == [1, 1]


# -- D6: DetailPanel.mount(ui, vm) + ui.set("sel", key) renders a real item -- #

@need_node
def test_detail_panel_mount_renders_selected_item_title(tmp_path):
    out = run_node(tmp_path, "details")
    assert out["hiddenBefore"] is True        # no selection yet: sheet starts hidden
    assert out["shownAfter"] is False         # selection reveals the panel
    assert out["title"] in out["knownText"], "item title not rendered"
    assert "Not in this snapshot." not in out["knownText"]
    # unknown key keeps the fallback message (the negative control)
    assert "Not in this snapshot." in out["unknownText"]
    assert out["title"] not in out["unknownText"]


# ---------------------- statics: D2 / D5b / D7 wiring ----------------------- #

def test_template_has_no_link_tag_and_sidebar_close_control():
    """T2c: the template carries NO <link> tag (self-containment guard stays,
    hence no favicon at all) alongside the D5b phone-only #sb-close control."""
    tpl = (WEB / "page_template.html").read_text(encoding="utf-8")
    assert "<link" not in tpl.lower() and 'rel="icon"' not in tpl, (
        "self-contained artifact: no <link> tag anywhere and no rel=\"icon\""
    )
    assert 'id="sb-close"' in tpl
    assert '<button id="sb-close"' in tpl, "close control must be a real button"
    assert 'id="sidebar"' in tpl            # it belongs to the sidebar overlay


def test_overlay_close_moves_focus_and_escape_matches_sb_close():
    """T2c a11y: leaving the phone overlay hands focus to a visible element,
    and Esc leaves it through the same path as #sb-close."""
    boot = (WEB / "page_boot.js").read_text(encoding="utf-8")
    assert "focusAfter()" in boot, "close must move focus to details heading / Graph tab"
    assert 'addEventListener("click", closeOverlay)' in boot and (
        'if (is("inbox") || is("menu")) closeOverlay(); else clearAll();' in boot
    ), "Esc must close the overlay via the shared #sb-close path"


def test_boot_exposes_store_alias_and_keeps_ui_and_jump():
    """D7: PlanUI.app carries store === ui alongside ui and jump."""
    boot = (WEB / "page_boot.js").read_text(encoding="utf-8")
    assert "store: ui" in boot
    assert "ui: ui" in boot
    assert "jump:" in boot or "jump :" in boot
