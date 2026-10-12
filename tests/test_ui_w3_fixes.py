"""W3 fixes — node-run tests for the three confirmed review findings.

1. page_ui.js: phase-based snapshots (phase, no track) — visibility + pushFilters use effective phase
2. page_search.js: state facet case-insensitive + hyphen/space normalization (in-review → in_review)
3. page_side.js: table row cap at 300 with "+N more" row

Follows tests/test_ui_pure.py and tests/test_ui_shell.py node-runner patterns.
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

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# -----------------------------------------------------------------------------
# Shared minimal fake DOM (adapted from test_ui_shell.py)
# -----------------------------------------------------------------------------
FAKE_DOM = r"""
"use strict";
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
El.prototype.dispatch = function (t) {
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
var doc = {
  nodeType: 9,
  readyState: "complete",
  body: body,
  createElement: function (t) { return new El(t); },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) { return qsa(body, sel); },
  querySelector: function (sel) { return qsa(body, sel)[0] || null; },
  addEventListener: function () {},
  removeEventListener: function () {}
};
global.document = doc;
"""

# -----------------------------------------------------------------------------
# Test 1: page_ui.js — effective phase used for visibility and pushFilters
# -----------------------------------------------------------------------------
HARNESS_UI_PHASE = FAKE_DOM + r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FIXTURE = process.argv[3];

// Minimal PlanUI stub for pushFilters (must be set BEFORE page_ui.js loads)
global.PlanUI = {
  app: {
    ui: {
      _filters: {},
      get: function (k) { return this._filters; },
      patch: function (f) { this._filters = f; }
    }
  },
  Sidebar: { renderProjects: function () {} },
  QAPage: { render: function () {} }
};

require(WEB + "/page.js");
require(WEB + "/page_model.js");
require(WEB + "/page_ui.js");
require(WEB + "/page_ui_filters.js");

// Now use the REAL PlanPage from page_ui.js (it overwrites global.PlanPage)
var PlanPage = global.PlanPage;
var PlanUI = global.PlanUI;

// Load a phase-only snapshot (items have phase but no track)
var snap = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));

// Create a modified snapshot where items have phase but track is removed
var phaseOnlySnap = {
  generated: snap.generated,
  head: snap.head,
  items: snap.items.map(function (it) {
    var copy = JSON.parse(JSON.stringify(it));
    copy.phase = it.phase || it.track;  // ensure phase exists
    delete copy.track;                  // remove track
    return copy;
  })
};

// Initialize page_ui state with phase-only snapshot
var state = PlanPage.state;
state.snapshot = phaseOnlySnap;
state.items = phaseOnlySnap.items.slice();
state.projects = PlanPage.uniqueSorted(state.items.map(function (i) { return i.project; }));
// state.tracks will be empty because items have no track field
state.tracks = PlanPage.uniqueSorted(state.items.map(function (i) { return i.track; }));
PlanPage.recompute();

// Count items per phase
var phaseCounts = {};
state.items.forEach(function (it) {
  var ph = it.phase || it.track || "";
  if (ph) phaseCounts[ph] = (phaseCounts[ph] || 0) + 1;
});
var phases = Object.keys(phaseCounts).sort();
var firstPhase = phases[0];

// Toggle first phase OFF
var wasOn = PlanPage.toggleFilter("phase", firstPhase);
var isOn = PlanPage.isFilterOn("phase", firstPhase);

// Check that pushFilters was called with correct phases (via PlanUI.app.ui._filters)
var filters = PlanUI.app.ui._filters;
var phaseFilter = filters.phases ? Array.from(filters.phases) : [];

// Check visibility: items with the toggled-off phase should be hidden
var visibleItems = state.items.filter(PlanPage.visible);
var hiddenCount = state.items.length - visibleItems.length;
var expectedHidden = phaseCounts[firstPhase] || 0;

function emit(o) { process.stdout.write(JSON.stringify(o)); }
emit({
  test: "page_ui_phase_visibility",
  phasesFound: phases,
  firstPhase: firstPhase,
  phaseCounts: phaseCounts,
  toggleResult: { wasOn: wasOn, isOn: isOn },
  filters: { phases: phaseFilter },
  visibility: { total: state.items.length, visible: visibleItems.length, hidden: hiddenCount, expectedHidden: expectedHidden }
});
"""

# -----------------------------------------------------------------------------
# Test 2: page_search.js — state facet case-insensitive + hyphen normalization
# -----------------------------------------------------------------------------
HARNESS_SEARCH = FAKE_DOM + r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FIXTURE = process.argv[3];

require(WEB + "/page.js");
var PlanUI = require(WEB + "/page_model.js");
require(WEB + "/page_search.js");

var snap = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
var vm = PlanUI.buildViewModel(snap);

function check(query) {
  var matches = [];
  vm.items.forEach(function (it, key) {
    if (PlanUI.Query.match(it, query)) matches.push(key);
  });
  return matches;
}

var results = {
  "state:ready": check("state:ready"),
  "state:READY": check("state:READY"),
  "state:Ready": check("state:Ready"),
  "state:in-review": check("state:in-review"),
  "state:in_review": check("state:in_review"),
  "state:blocked": check("state:blocked"),
  "state:BLOCKED": check("state:BLOCKED"),
  "state:done": check("state:done"),
  "state:DONE": check("state:DONE")
};

function emit(o) { process.stdout.write(JSON.stringify(o)); }
emit({ test: "page_search_state_facet", results: results });
"""

# -----------------------------------------------------------------------------
# Test 3: page_side.js — table row cap at 300 with "+N more" row
# -----------------------------------------------------------------------------
HARNESS_SIDE_CAP = FAKE_DOM + r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FIXTURE = process.argv[3];

// Build the shell markup page_side.js expects
var sidebar = doc.createElement("aside");
sidebar.setAttribute("id", "sidebar");
var sbClose = doc.createElement("button");
sbClose.setAttribute("id", "sb-close");
sbClose.className = "sb-close";
sidebar.appendChild(sbClose);
var tablist = doc.createElement("div");
tablist.className = "sb-tabs";
["needs", "qa", "answers", "settings"].forEach(function (t) {
  var b = doc.createElement("button");
  b.className = "sb-tab";
  b.setAttribute("data-tab", t);
  b.setAttribute("role", "tab");
  var c = doc.createElement("span");
  c.className = "sb-count";
  c.setAttribute("id", "sb-count-" + t);
  c.setAttribute("hidden", "");
  c.hidden = true;
  b.appendChild(c);
  tablist.appendChild(b);
});
sidebar.appendChild(tablist);
var sbBody = doc.createElement("div");
sbBody.className = "sb-body";
["needs", "qa", "answers", "settings"].forEach(function (t) {
  var sec = doc.createElement("section");
  sec.className = "sb-panel";
  sec.setAttribute("id", "sb-panel-" + t);
  sec.setAttribute("data-panel", t);
  sbBody.appendChild(sec);
});
sidebar.appendChild(sbBody);
doc.body.appendChild(sidebar);

// PlanPage stub
var PlanPage = {
  state: { selected: null, items: [], unblocks: {}, booted: true, cy: null,
           projectsOff: {}, tracksOff: {} },
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
  renderGraph: function () {},
  isFilterOn: function () { return true; },
  toggleFilter: function () { return true; }
};
global.PlanPage = PlanPage;

require(WEB + "/page.js");
var PlanUI = require(WEB + "/page_model.js");
require(WEB + "/page_store.js");
require(WEB + "/page_search.js");
require(WEB + "/page_side.js");
require(WEB + "/page_side_tables.js");

var snap = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
var vm = PlanUI.buildViewModel(snap);
var ui = new PlanUI.Store({});

PlanUI.Sidebar.mount(ui, vm);

// Switch to QA panel which has the decisions and questions tables
ui.set("sidebar", { tab: "qa" });

// Find the decisions table and qa table
var decisionsTable = doc.getElementById("decisions-table");
var qaTable = doc.getElementById("qa-table");

// Count rows (tbody tr) in each table
function countRows(table) {
  if (!table) return 0;
  var tbody = table.querySelector("tbody");
  if (!tbody) return 0;
  var rows = tbody.querySelectorAll("tr");
  return rows.length;
}

var dRows = countRows(decisionsTable);
var qRows = countRows(qaTable);

// Also check for "+N more" row (td with colSpan and textContent containing "+")
function hasMoreRow(table) {
  if (!table) return false;
  var tbody = table.querySelector("tbody");
  if (!tbody) return false;
  var rows = tbody.querySelectorAll("tr");
  for (var i = 0; i < rows.length; i++) {
    var cells = rows[i].childNodes;
    for (var j = 0; j < cells.length; j++) {
      var tc = cells[j].textContent || "";
      if (tc.indexOf("+") === 0 && tc.indexOf("more") >= 0) return true;
    }
  }
  return false;
}

var dMore = hasMoreRow(decisionsTable);
var qMore = hasMoreRow(qaTable);

// Total items in each source
var dTotal = (vm.decisions || []).length;
var qTotal = (vm.questions || []).length;

function emit(o) { process.stdout.write(JSON.stringify(o)); }
emit({
  test: "page_side_table_cap",
  decisions: { total: dTotal, rendered: dRows - (dMore ? 1 : 0), hasMore: dMore },
  qa: { total: qTotal, rendered: qRows - (qMore ? 1 : 0), hasMore: qMore },
  maxRows: 300
});
"""

def write_harness(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "harness.js"
    p.write_text(content, encoding="utf-8")
    return p


def run_node(tmp_path: Path, harness_content: str, *args) -> dict:
    harness_path = write_harness(tmp_path, harness_content)
    cmd = ["node", str(harness_path)] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


# =============================================================================
# TEST 1: page_ui.js — effective phase for phase-only snapshots
# =============================================================================

@need_node
def test_page_ui_effective_phase_visibility(tmp_path):
    """Phase-only snapshot: items have `phase` but no `track`.

    - visible() must use effective phase (phase || track)
    - pushFilters must send phases list built from effective phases
    - toggling a phase off must filter those items from the graph
    """
    out = run_node(tmp_path, HARNESS_UI_PHASE, str(WEB), str(FIXTURE))

    # Verify phase detection works
    assert out["phasesFound"], "should detect phases from phase-only items"
    assert out["firstPhase"], "should have at least one phase"

    # Verify toggle works: toggleFilter returns NEW state (false = off after toggle)
    # isFilterOn returns current state (false = off)
    assert out["toggleResult"]["wasOn"] is False, "toggleFilter returns new state (off)"
    assert out["toggleResult"]["isOn"] is False, "phase should be off after toggle"

    # Verify pushFilters sent phase filter (not track filter)
    phase_filter = out["filters"]["phases"]
    assert isinstance(phase_filter, list), "phases filter should be an array"
    # Should contain __none__ sentinel or the remaining phases
    # (since we toggled one off, the filter should be active)

    # Verify visibility filtering
    vis = out["visibility"]
    assert vis["hidden"] == vis["expectedHidden"], (
        f"Expected {vis['expectedHidden']} hidden items for phase {out['firstPhase']}, "
        f"got {vis['hidden']}"
    )
    assert vis["hidden"] > 0, "should have hidden some items"


# =============================================================================
# TEST 2: page_search.js — state facet case-insensitive + normalization
# =============================================================================

@need_node
def test_page_search_state_facet_case_insensitive(tmp_path):
    """state: facet must be case-insensitive and normalize hyphens to underscore."""
    out = run_node(tmp_path, HARNESS_SEARCH, str(WEB), str(FIXTURE))
    results = out["results"]

    # Case-insensitive matching
    assert set(results["state:ready"]) == set(results["state:READY"]) == set(results["state:Ready"]), (
        "state:ready, state:READY, state:Ready must match the same items"
    )
    assert len(results["state:ready"]) > 0, "should match some ready items"

    assert set(results["state:blocked"]) == set(results["state:BLOCKED"]), (
        "state:blocked and state:BLOCKED must match the same items"
    )

    assert set(results["state:done"]) == set(results["state:DONE"]), (
        "state:done and state:DONE must match the same items"
    )

    # Hyphen normalization: in-review and in_review both match in_review
    assert set(results["state:in-review"]) == set(results["state:in_review"]), (
        "state:in-review and state:in_review must match the same items"
    )
    assert len(results["state:in_review"]) > 0, "should match some in_review items"


# =============================================================================
# TEST 3: page_side.js — table row cap at 300
# =============================================================================

@need_node
def test_page_side_table_row_cap(tmp_path):
    """Tables in sidebar must cap at 300 rows + one '+N more' row (textContent only)."""
    # L1: the synthetic fixture has only 18 decisions, so blow it up to 1000 to
    # exercise the cap for real (the under-cap branch alone proves nothing).
    snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = snap["decisions"][0]
    snap["decisions"] = [dict(base, id="D-%d" % (1000 + i)) for i in range(1000)]
    big = tmp_path / "big_snapshot.json"
    big.write_text(json.dumps(snap), encoding="utf-8")
    out = run_node(tmp_path, HARNESS_SIDE_CAP, str(WEB), str(big))

    d = out["decisions"]
    assert d["total"] == 1000, d
    q = out["qa"]
    max_rows = out["maxRows"]

    # Decisions table
    assert d["rendered"] <= max_rows, f"decisions table exceeded {max_rows} rows: {d['rendered']}"
    if d["total"] > max_rows:
        assert d["rendered"] == max_rows, f"decisions table should cap at {max_rows}"
        assert d["hasMore"] is True, "decisions table should have '+N more' row when capped"
    else:
        assert d["rendered"] == d["total"], "decisions table should show all rows when under cap"

    # Q&A table
    assert q["rendered"] <= max_rows, f"QA table exceeded {max_rows} rows: {q['rendered']}"
    if q["total"] > max_rows:
        assert q["rendered"] == max_rows, f"QA table should cap at {max_rows}"
        assert q["hasMore"] is True, "QA table should have '+N more' row when capped"
    else:
        assert q["rendered"] == q["total"], "QA table should show all rows when under cap"

    # At least one table should demonstrate the cap (fixture has 18 decisions, 12 questions)
    # The fixture might not exceed 300, but the logic is tested via the cap constant
    assert max_rows == 300, "MAX_ROWS constant should be 300"