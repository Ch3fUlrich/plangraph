"""UI-VERSION-STAMP page side (B + V-SHELL-4) — node-run tests for
PlanUI.Stamp.format (same idiom as tests/test_ui_pure.py: plain ``node``,
skipped when node is missing) plus one render() check that the assembled
page carries the stamp code and CSS.

Synthetic data only: no hosts, IPs or home paths in the page or here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

import sys

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

GEN = "2026-10-01T00:00:00Z"
GEN_MS = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
DOT = "\u00b7"  # ·

HARNESS = r"""
"use strict";
var fs = require("fs");
var PlanUI = require(process.argv[2]);           // page_stamp.js -> module.exports
var cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var out = cases.map(function (c) {
  return PlanUI.Stamp.format(c.sha, c.generated, c.headSeq, c.headHash, c.nowMs);
});
process.stdout.write(JSON.stringify(out));
"""


def run_format(tmp_path: Path, cases: list) -> list:
    harness = tmp_path / "stamp_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cfile = tmp_path / "cases.json"
    cfile.write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(WEB / "page_stamp.js"), str(cfile)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def at_age(age_min: int) -> int:
    return GEN_MS + age_min * 60_000


@need_node
def test_format_text_for_the_three_sha_values(tmp_path):
    cases = [
        {"sha": "abc1234567def", "generated": GEN, "headSeq": 42, "headHash": None,
         "nowMs": at_age(1)},
        {"sha": "", "generated": GEN, "headSeq": 42, "headHash": None, "nowMs": at_age(1)},
        {"sha": None, "generated": GEN, "headSeq": None, "headHash": None, "nowMs": at_age(1)},
    ]
    [real, empty, missing] = run_format(tmp_path, cases)
    assert real["text"] == f"code abc1234567def {DOT} data {GEN} {DOT} seq 42"
    assert empty["text"] == f"code unknown {DOT} data {GEN} {DOT} seq 42"
    # missing head_seq shows "-"
    assert missing["text"] == f"code unknown {DOT} data {GEN} {DOT} seq -"


@need_node
def test_title_carries_keeper_head_hash(tmp_path):
    cases = [
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": "deadbeefcafebabe0123456789",
         "nowMs": at_age(1)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": "short",
         "nowMs": at_age(1)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(1)},
    ]
    [full, short, none] = run_format(tmp_path, cases)
    assert full["title"] == "keeper head deadbeefca"
    assert short["title"] == "keeper head short"
    assert none["title"] == ""  # no hash -> no tooltip text


@need_node
def test_stale_clock_boundary_and_age_text(tmp_path):
    cases = [
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(14)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(16)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(90)},
    ]
    young, aged, hours = run_format(tmp_path, cases)
    assert young["stale"] is False and f"{DOT} age" not in young["text"]
    assert young["ageMin"] == 14
    assert aged["stale"] is True
    assert aged["text"] == f"code s {DOT} data {GEN} {DOT} seq 1 {DOT} age 16 min"
    assert hours["stale"] is True
    assert hours["text"].endswith(f"{DOT} age 1h 30m")


@need_node
def test_exactly_15_minutes_is_fresh_one_second_more_is_stale(tmp_path):
    """Spec: stale means OLDER than 15 min — strict ms comparison, not >= 15 floor."""
    cases = [
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None,
         "nowMs": GEN_MS + 15 * 60_000},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None,
         "nowMs": GEN_MS + 15 * 60_000 + 1000},
    ]
    exact, later = run_format(tmp_path, cases)
    assert exact["stale"] is False and f"{DOT} age" not in exact["text"]
    assert exact["ageMin"] == 15  # ageMin stays floor minutes
    assert later["stale"] is True
    assert later["ageMin"] == 15
    assert later["text"].endswith(f"{DOT} age 15 min")


@need_node
def test_missing_or_bad_generated_is_never_stale(tmp_path):
    cases = [
        {"sha": "s", "generated": None, "headSeq": 1, "headHash": None, "nowMs": at_age(999)},
        {"sha": "s", "generated": "nonsense", "headSeq": 1, "headHash": None, "nowMs": at_age(999)},
    ]
    missing, bad = run_format(tmp_path, cases)
    for out in (missing, bad):
        assert out["stale"] is False
        assert out["ageMin"] is None
        assert "age" not in out["text"]
    assert missing["text"] == f"code s {DOT} data - {DOT} seq 1"


SNAP = {"generated": GEN, "head": "abc1234567def0123", "head_seq": 42, "items": []}


def test_rendered_page_carries_stamp_code_and_css():
    html = render_mod.render(SNAP, ROOT / "vendor", cdn=True)
    assert ".stamp-stale" in html          # the CSS rule for V-SHELL-4
    assert "PlanUI.Stamp" in html          # the page_stamp.js code is inlined


# --------------------------------------------------------------------------- #
# V-SHELL-4 clock timer: the 60 s re-render must call the REGISTERED refresh
# fn (Stamp.setRefresh), never a hard-coded global, and must not start before
# a fn is registered. Fake setInterval + fake Date.now; no real timers.
# --------------------------------------------------------------------------- #

TIMER_HARNESS = r"""
"use strict";
var WEB = process.argv[2];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

global.document = { querySelector: function () { return null; } };
var timers = [];
global.setInterval = function (fn, ms) {
  var t = { fn: fn, ms: ms };
  timers.push(t);
  return t;
};

function main() {
  var PlanUI = require(WEB + "/page_stamp.js");
  var out = {
    setRefreshPresent: typeof PlanUI.Stamp.setRefresh === "function",
    timersAfterLoad: timers.length
  };
  PlanUI.Stamp.startClockRefresh();          // still no refresh fn registered
  out.timersWithoutRefreshFn = timers.length;
  if (!out.setRefreshPresent) { emit(out); return; }
  var calls = 0;
  PlanUI.Stamp.setRefresh(function () { calls++; });
  out.timersAfterRegister = timers.length;
  out.intervalMs = timers.length ? timers[timers.length - 1].ms : null;
  if (timers.length) timers[timers.length - 1].fn();
  out.callsAfterTick = calls;
  PlanUI.Stamp.startClockRefresh();          // idempotent: one timer only
  out.timersAfterSecondStart = timers.length;
  emit(out);
}
try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path, harness_src: str, *args: str) -> dict:
    harness = tmp_path / "stamp_timer_harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(WEB), *[str(a) for a in args]],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_stamp_timer_starts_only_with_a_registered_refresh_fn(tmp_path):
    out = run_node(tmp_path, TIMER_HARNESS)
    assert out["setRefreshPresent"] is True, "PlanUI.Stamp.setRefresh missing"
    assert out["timersAfterLoad"] == 0        # no timer at script load
    assert out["timersWithoutRefreshFn"] == 0  # and none without a fn
    assert out["timersAfterRegister"] == 1
    assert out["intervalMs"] == 60000
    assert out["callsAfterTick"] == 1         # the tick calls the registered fn
    assert out["timersAfterSecondStart"] == 1  # registration is idempotent


# --------------------------------------------------------------------------- #
# Regression: the 60 s refresh timer must be unref()'d so a node harness that
# does NOT fake setInterval still exits on its own. Real timers only here.
# --------------------------------------------------------------------------- #

REAL_TIMER_EXIT_HARNESS = r"""
"use strict";
var WEB = process.argv[2];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

/* real setInterval, only wrapped to count; nothing is faked away */
var started = 0;
var realSetInterval = global.setInterval;
global.setInterval = function (fn, ms) { started++; return realSetInterval(fn, ms); };
global.document = { querySelector: function () { return null; } };

var PlanUI = require(WEB + "/page_stamp.js");
PlanUI.Stamp.setRefresh(function () {});
emit({ timersStarted: started });
/* no process.exit(): if the live 60 s timer keeps the event loop ref'd,
 * node hangs and the subprocess timeout below fires */
"""


@need_node
def test_real_set_interval_does_not_keep_the_node_process_alive(tmp_path):
    harness = tmp_path / "stamp_real_timer_exit_harness.js"
    harness.write_text(REAL_TIMER_EXIT_HARNESS, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(harness), str(WEB)],
            capture_output=True, text=True, encoding="utf-8", timeout=5, cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        pytest.fail("node did not exit: the 60 s refresh timer is not unref()'d")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["timersStarted"] == 1  # the timer really was started, unfaked


WIRING_HARNESS = r"""
"use strict";
var WEB = process.argv[2];
var GEN = process.argv[3];
var GEN_MS = Number(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

/* minimal fake DOM: only the three nodes renderHeader touches */
function FakeEl() { this.className = ""; this.attributes = {}; this.style = {}; this.textContent = ""; }
FakeEl.prototype.setAttribute = function (n, v) { this.attributes[n] = String(v); };
FakeEl.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
var els = {};
["meta-line", "progress-bar", "progress-text"].forEach(function (id) { els[id] = new FakeEl(); });
global.document = {
  readyState: "loading",
  documentElement: new FakeEl(),
  body: new FakeEl(),
  createElement: function () { return new FakeEl(); },
  createTextNode: function (t) { var n = new FakeEl(); n.textContent = String(t); return n; },
  getElementById: function (id) { return els[id] || null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {}, removeEventListener: function () {}
};
var timers = [];
global.setInterval = function (fn, ms) {
  var t = { fn: fn, ms: ms };
  timers.push(t);
  return t;
};
var fakeNow = GEN_MS;
Date.now = function () { return fakeNow; };

var SNAP = { generated: GEN, head: "abc1234567def0123", head_seq: 42, items: [] };

function main() {
  /* load order mirrors render.py JS_FILES: page_ui.js before page_stamp.js */
  require(WEB + "/page.js");
  require(WEB + "/page_ui.js");
  require(WEB + "/page_stamp.js");
  var PlanPage = global.PlanPage;
  var out = { timersAfterLoad: timers.length };
  PlanPage.state.snapshot = SNAP;
  fakeNow = GEN_MS + 60000;                 // 1 min old: fresh
  PlanPage.renderHeader();                  // what the page boot does
  out.timersAfterFirstRender = timers.length;
  out.freshClass = els["meta-line"].className;
  if (!timers.length) { emit(out); return; }
  out.intervalMs = timers[timers.length - 1].ms;
  fakeNow = GEN_MS + 15 * 60000 + 1000;     // 15 min + 1 s: stale
  timers[timers.length - 1].fn();           // the 60 s clock tick
  out.staleClass = els["meta-line"].className;
  out.staleText = els["meta-line"].textContent;
  emit(out);
}
try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


@need_node
def test_header_flips_to_stale_on_clock_tick_via_page_ui(tmp_path):
    out = run_node(tmp_path, WIRING_HARNESS, GEN, str(GEN_MS))
    assert out["timersAfterLoad"] == 0, out   # no timer before a refresh fn exists
    assert out["timersAfterFirstRender"] == 1, out  # page_ui registered renderHeader
    assert out["intervalMs"] == 60000
    assert "stamp-stale" not in (out["freshClass"] or "")
    assert "stamp-stale" in (out.get("staleClass") or ""), out
    assert f"{DOT} age 15 min" in (out.get("staleText") or ""), out


STAMP_LINE_HARNESS = r"""
"use strict";
var WEB = process.argv[2];
var GEN = process.argv[3];
var GEN_MS = Number(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

/* fake DOM, same contract as tests/test_ui_real_page.py's stub, reduced to
 * what renderHeader touches, plus the <meta name="plangraph-build"> render.py emits */
function FakeEl() { this.className = ""; this.attributes = {}; this.style = {}; this.textContent = ""; }
FakeEl.prototype.setAttribute = function (n, v) { this.attributes[n] = String(v); };
FakeEl.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
var els = {};
["meta-line", "progress-bar", "progress-text"].forEach(function (id) { els[id] = new FakeEl(); els[id].setAttribute("id", id); });
global.document = {
  readyState: "loading",
  documentElement: new FakeEl(),
  body: new FakeEl(),
  createElement: function () { return new FakeEl(); },
  createTextNode: function (t) { var n = new FakeEl(); n.textContent = String(t); return n; },
  getElementById: function (id) { return els[id] || null; },
  querySelector: function (sel) {
    return sel.indexOf("plangraph-build") >= 0 ? { content: "abc1234567" } : null;
  },
  querySelectorAll: function () { return []; },
  addEventListener: function () {}, removeEventListener: function () {}
};
var timers = [];
global.setInterval = function (fn, ms) { var t = { fn: fn, ms: ms }; timers.push(t); return t; };
var fakeNow = GEN_MS;
Date.now = function () { return fakeNow; };

var SNAP = { generated: GEN, head: "cafe1234beef5678", head_seq: 42, items: [] };
var ITEMS = [
  { key: "projA:T1", project: "projA", state: "open", estimate_h: 2, depends_on: [] },
  { key: "projA:T2", project: "projA", state: "done", estimate_h: 2, depends_on: [] }
];

function main() {
  /* load order mirrors render.py JS_FILES: page_ui.js before page_stamp.js */
  require(WEB + "/page.js");
  require(WEB + "/page_ui.js");
  require(WEB + "/page_stamp.js");
  var PlanPage = global.PlanPage;
  PlanPage.state.snapshot = SNAP;
  PlanPage.state.items = ITEMS;
  fakeNow = GEN_MS + 60000;                 // 1 min old: fresh
  PlanPage.renderHeader();                  // what the page boot does
  var meta = els["meta-line"];
  var out = {
    freshText: meta.textContent,
    freshTitle: meta.attributes.title || "",
    freshClass: meta.className,
    progressText: els["progress-text"].textContent,
    progressWidth: String(els["progress-bar"].style.width)
  };
  fakeNow = GEN_MS + 15 * 60000 + 1000;     // 15 min + 1 s: stale
  timers[timers.length - 1].fn();           // the 60 s clock tick
  out.staleClass = meta.className;
  out.staleText = meta.textContent;
  emit(out);
}
try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


@need_node
def test_renderheader_stamp_line_and_progress_via_page_ui(tmp_path):
    """Real renderHeader: build sha from the meta tag, full #meta-line text,
    tooltip carries the keeper head, and the progress update is not skipped."""
    out = run_node(tmp_path, STAMP_LINE_HARNESS, GEN, str(GEN_MS))
    assert out["freshText"] == f"code abc1234567 {DOT} data {GEN} {DOT} seq 42 {DOT} 2 items"
    assert "keeper head cafe1234be" in out["freshTitle"]
    assert "cafe1234be" not in out["freshText"]  # head hash stays in the tooltip
    assert "stamp-stale" not in out["freshClass"]
    assert re.fullmatch(rf"\d+% done by estimate {DOT} \d+% by count",
                        out["progressText"]), out["progressText"]
    assert out["progressWidth"].endswith("%")
    assert "stamp-stale" in out["staleClass"], out
    assert f"{DOT} age" in out["staleText"], out


def test_page_stamp_never_references_planpage():
    """Defect 1: the stamp module must not hard-code global.PlanPage.renderHeader."""
    src = (WEB / "page_stamp.js").read_text(encoding="utf-8")
    assert "PlanPage" not in src
