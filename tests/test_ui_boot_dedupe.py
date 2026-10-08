"""W2 boot dedupe + phone Settings reachability.

Follows tests/test_ui_shell.py's pattern: a minimal fake DOM written inside a
node harness (no jsdom, no packages), skipped cleanly when node is missing,
plus pure-python static probes of page_boot.js / page_template.html.

Covers:
  DEDUPE  page_boot.js declares each of the 16 formerly-duplicated functions
          exactly once (the second, fully-wired copy is the one kept), stays
          at most 400 lines, and keeps the full bindControls wiring
          (sb-toggle, scrim click, sb-close, #tabbar, keydown);
  PHONE   page_template.html's #tabbar carries a settings entry
          (data-screen="settings") routed by the existing generic tabbar
          click handler through PlanUI.Pages.show("settings");
  CLICK   under node, dispatching a click on that tabbar settings button
          (via the same handler shape bindControls installs) shows
          panel-settings and hides the other panels (real page_pages.js).
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

# The 16 names the task states were declared twice (lines ~124-297 dead copy).
NAMES = [
    "setScreen", "syncView", "resizeGraph", "writeHash", "typing",
    "nextReady", "toggleExpand", "collapsePhase", "moveSelection",
    "clearAll", "onKey", "focusSearch", "focusAfter", "closeOverlay",
    "debounced", "bindControls",
]

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def boot_src() -> str:
    return (WEB / "page_boot.js").read_text(encoding="utf-8")


def template_src() -> str:
    return (WEB / "page_template.html").read_text(encoding="utf-8")


def tabbar_block() -> str:
    tpl = template_src()
    m = re.search(r'<nav[^>]*id="tabbar"[^>]*>(.*?)</nav>', tpl, re.DOTALL | re.IGNORECASE)
    assert m, "#tabbar nav not found in page_template.html"
    return m.group(1)


# ------------------------------- dedupe ---------------------------------- #

def test_boot_declares_each_function_once():
    src = boot_src()
    for name in NAMES:
        found = re.findall(r"\bfunction\s+" + re.escape(name) + r"\s*\(", src)
        assert len(found) == 1, f"{name}: declared {len(found)}x, want exactly 1"


def test_boot_stays_within_line_budget():
    n = len(boot_src().splitlines())
    assert n <= 400, f"page_boot.js is {n} lines, budget is 400"


def test_boot_keeps_full_bindcontrols_wiring():
    """The kept copy is the second (fully-wired) one, not the truncated paste."""
    src = boot_src()
    assert '$("sb-toggle")' in src, "sb-toggle wiring missing (truncated copy kept?)"
    assert 'scrim.addEventListener("click"' in src, "scrim click wiring missing"
    assert '"sb-close"' in src and "closeOverlay" in src, "sb-close wiring missing"
    assert '#tabbar .tab-btn' in src, "tabbar wiring missing"
    assert 'document.addEventListener("keydown", onKey)' in src, "keydown wiring missing"
    assert "PlanUI.Pages.show(target)" in src or "PlanUI.Pages.show( target )" in src or \
        'PlanUI.Pages) PlanUI.Pages.show(target)' in src, \
        "generic tabbar -> Pages.show(target) routing missing"


# --------------------------- phone reachability ------------------------- #

def test_tabbar_contains_settings_entry():
    bar = tabbar_block()
    assert 'data-screen="settings"' in bar, \
        "no Settings entry in #tabbar: Settings unreachable at 390x844 phone width"
    m = re.search(
        r'<button[^>]*data-screen="settings"[^>]*>(.*?)</button>',
        bar, re.DOTALL | re.IGNORECASE,
    )
    assert m, "settings tabbar entry is not a <button>"
    tag = m.group(0)
    inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    assert 'aria-label="Settings"' in tag or 'aria-label=\'Settings\'' in tag \
        or inner.lower() == "settings", \
        "settings tabbar button needs label 'Settings' or aria-label"
    # The bottom nav must stay compact: 6 entries max so nothing overflows 390 px.
    assert len(re.findall(r"<button\b", bar, re.IGNORECASE)) <= 6


# ---------------- node: click settings -> panel-settings ----------------- #
# Mirrors test_ui_shell.py's fake DOM; loads the REAL page_pages.js router and
# installs the SAME generic handler shape page_boot.js bindControls uses, then
# clicks the tabbar settings button taken from the template.

HARNESS = r"""
"use strict";
var WEB = process.argv[2];
var TARGET = process.argv[3] || "settings";
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var ids = {};
function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.attributes = {};
  this.listeners = {};
  this.hidden = false;
  this.className = "";
  this.classList = {
    add: function () {}, remove: function () {},
    toggle: function () {}, contains: function () { return false; }
  };
}
Object.defineProperty(El.prototype, "firstChild", {
  get: function () { return this.childNodes.length ? this.childNodes[0] : null; }
});
El.prototype.appendChild = function (c) {
  c.parentNode = this; this.childNodes.push(c); return c;
};
El.prototype.setAttribute = function (n, v) {
  this.attributes[n] = String(v);
  if (n === "id") ids[String(v)] = this;
};
El.prototype.getAttribute = function (n) {
  return (n in this.attributes) ? this.attributes[n] : null;
};
El.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.dispatch = function (t, extra) {
  (this.listeners[t] || []).forEach(function (f) { f(extra || {type: t}); });
};

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
  var tag = rest.replace(/\.[\w-]+/g, "").replace(/#[\w-]+/g, "");
  if (tag && tag !== "*") {
    if (node.tagName !== tag.toUpperCase()) return false;
  }
  return true;
}
function walk(node, out) {
  node.childNodes.forEach(function (c) { out.push(c); walk(c, out); });
  return out;
}
function ancestorOk(node, parts, k) {
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
body.setAttribute("data-screen", "graph");
var doc = {
  nodeType: 9,
  readyState: "complete",
  body: body,
  documentElement: new El("html"),
  createElement: function (t) { return new El(t); },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) {
    if (/#tabbar/.test(sel) && /\.tab-btn/.test(sel)) {
      return qsa(body, sel);
    }
    return qsa(body, sel);
  },
  querySelector: function (sel) { return qsa(body, sel)[0] || null; },
  addEventListener: function () {},
  removeEventListener: function () {}
};
function add(parent, tag, attrs) {
  var n = new El(tag);
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

/* panels + header tabs the real router toggles */
add(body, "section", [["id", "panel-graph"]]).hidden = false;
add(body, "section", [["id", "panel-list"]]).hidden = true;
add(body, "section", [["id", "panel-qa"]]).hidden = true;
add(body, "section", [["id", "panel-needs"]]).hidden = true;
add(body, "section", [["id", "panel-settings"]]).hidden = true;
[["tab-graph"], ["tab-list"], ["tab-qa"], ["tab-needs"], ["tab-settings"]].forEach(function (a) {
  var t = add(body, "button", [["id", a[0]]]);
  t.setAttribute("aria-selected", "false");
});
/* #tabbar as shipped in page_template.html (incl. the new settings entry) */
var tabbar = add(body, "nav", [["id", "tabbar"]]);
[["graph", "Graph"], ["table", "Table"], ["qa", "Q&A"],
 ["needs", "Needs You"], ["menu", "Filter"], ["settings", ""]].forEach(function (p) {
  var b = add(tabbar, "button", [["data-screen", p[0]]]);
  b.setAttribute("class", "tab-btn");
  b.className = "tab-btn";
  if (p[0] === "settings") b.setAttribute("aria-label", "Settings");
});

global.document = doc;
global.window = global;

function main() {
  require(WEB + "/page_pages.js");
  var PlanUI = global.PlanUI;
  if (!PlanUI || !PlanUI.Pages) { emit({error: "Pages missing"}); return; }
  /* same generic shape as page_boot.js bindControls installs */
  Array.prototype.forEach.call(doc.querySelectorAll("#tabbar .tab-btn"), function (b) {
    b.addEventListener("click", function () {
      var target = b.getAttribute("data-screen");
      if (target === "menu") {
        body.setAttribute("data-screen", "menu");
        return;
      }
      if (PlanUI.Pages) PlanUI.Pages.show(target);
      body.setAttribute("data-screen", target);
    });
  });
  var btns = doc.querySelectorAll("#tabbar .tab-btn");
  var hit = null;
  Array.prototype.forEach.call(btns, function (b) {
    if (b.getAttribute("data-screen") === TARGET) hit = b;
  });
  if (!hit) { emit({error: "no tabbar button for " + TARGET}); return; }
  hit.dispatch("click");
  function hidden(id) { var n = doc.getElementById(id); return n ? !!n.hidden : null; }
  emit({target: TARGET,
        dataScreen: body.getAttribute("data-screen"),
        hidden: {graph: hidden("panel-graph"), table: hidden("panel-list"),
                 qa: hidden("panel-qa"), needs: hidden("panel-needs"),
                 settings: hidden("panel-settings")}});
}
try { main(); } catch (e) { emit({error: String((e && e.stack) || e)}); }
"""


def write_harness(tmp_path: Path) -> Path:
    p = tmp_path / "boot_dedupe_harness.js"
    p.write_text(HARNESS, encoding="utf-8")
    return p


@need_node
def test_clicking_tabbar_settings_shows_panel_settings(tmp_path):
    target = 'settings'
    assert f'data-screen="{target}"' in tabbar_block()
    cmd = ["node", str(write_harness(tmp_path)), str(WEB), target]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    assert out["dataScreen"] == "settings"
    assert out["hidden"]["settings"] is False, "panel-settings still hidden after click"
    for key in ("graph", "table", "qa", "needs"):
        assert out["hidden"][key] is True, f"panel {key} should hide when settings shows"
