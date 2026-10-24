"""PROGRESS-PANEL (D-547): views_progress.progress, the keeper doors and the page.

Mutations killed: counting a gate/goal (totals), counting cancelled work
(totals), hours from the done count instead of estimates (pct_hours), losing
the writer model on a running row, dropping the project filter.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from door_util import Doors
from keeper_util import World, defn
from plangraph.keeper import fmt
from plangraph.views_progress import progress

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _it(key, state="open", est=1, **kw):
    item = {"key": key, "project": key.split(":")[0], "title": key, "state": state,
            "kind": "lane", "estimate_h": est, "track": "T1", "operator": False}
    item.update(kw)
    return item


ITEMS = [
    _it("p:A", "done", 4, updated_at="2026-10-01T00:00:00Z"),
    _it("p:B", "claimed", 2, claimed_by="p-L1", writer_model="muse-spark",
        updated_at="2026-10-03T00:00:00Z"),
    _it("p:C", "in_review", 2, claimed_by="p-L1", track="T2"),
    _it("p:D", "open", 2, operator=True, track="T2"),
    _it("p:X", "cancelled", 50, updated_at="2026-10-05T00:00:00Z"),
    _it("p:G", "open", 9, kind="gate"), _it("p:GOAL", "open", 9, kind="goal"),
    _it("q:A", "done", 1),
]


def test_totals_skip_structure_and_cancelled():
    out = progress(ITEMS)
    p = out["projects"][0]
    assert [r["project"] for r in out["projects"]] == ["p", "q"]
    assert (p["done"], p["total"], p["pct_count"]) == (1, 4, 25.0)
    assert (p["hours_done"], p["hours_total"], p["hours_left"], p["pct_hours"]) == (4.0, 10.0, 6.0, 40.0)
    assert p["states"] == {"open": 1, "claimed": 1, "in_review": 1, "done": 1, "cancelled": 1}
    assert p["operator_open"] == 1
    assert p["last_update"] == "2026-10-05T00:00:00Z"   # a cancel is still an update
    assert out["total"]["total"] == 5 and out["total"]["done"] == 2


def test_phases_and_running():
    p = progress(ITEMS)["projects"][0]
    assert [(ph["phase"], ph["done"], ph["total"]) for ph in p["phases"]] == [("T1", 1, 2), ("T2", 0, 2)]
    assert p["running"] == [
        {"key": "p:B", "title": "p:B", "owner": "p-L1", "state": "claimed", "model": "muse-spark"},
        {"key": "p:C", "title": "p:C", "owner": "p-L1", "state": "in_review", "model": None}]


def test_project_filter_and_empty():
    assert [r["project"] for r in progress(ITEMS, project="q")["projects"]] == ["q"]
    assert progress([]) == {"projects": [], "total": {
        "total": 0, "done": 0, "hours_total": 0.0, "hours_done": 0.0, "hours_left": 0.0,
        "pct_count": 0.0, "pct_hours": 0.0}}


def test_text():
    text = fmt.progress_text(progress(ITEMS))
    assert text.splitlines()[0].startswith("p | 25.0% (1/4) | 40.0% h, 6.0 h left | open 1 claimed 1")
    assert "  running p:B | p-L1 | claimed | model muse-spark" in text
    assert "  running p:C | p-L1 | in_review | model unknown" in text
    assert text.splitlines()[-1] == "total | 40.0% (2/5) | 6.0 h left"
    assert fmt.progress_text({"projects": []}) == "no items"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"), defn("B", estimate_h=3), defn("G", ["A", "B"], kind="goal")])
    yield world
    world.close()


def test_store_snapshot_and_http(w, tmp_path):
    snap = w.store.snapshot(w.p("reader"))
    assert snap["progress"]["projects"][0]["total"] == 2
    assert w.store.progress(w.p("reader"))["projects"][0]["hours_total"] == 4.0
    assert w.store.progress(w.p("reader"), project="nope") == {"projects": [], "total": progress([])["total"]}
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("reader", "GET", "/v1/progress")
        assert status == 200 and body["projects"][0]["project"] == "demo"
        status, text = doors.http("reader", "GET", "/v1/progress?fmt=text")
        assert status == 200 and text.startswith("demo | 0.0% (0/2)")
    finally:
        doors.close()


PAGE_JS = r"""
"use strict";
var nodes = {};
function El(tag) { this.tag = tag; this.children = []; this._t = ""; this.style = {}; this.attrs = {}; }
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.removeChild = function (c) { this.children.splice(this.children.indexOf(c), 1); };
Object.defineProperty(El.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this._t + this.children.map(function (c) { return c.textContent; }).join("|"); },
  set: function (v) { this._t = String(v); } });
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.addEventListener = function () {};
global.document = { createElement: function (t) { return new El(t); },
                    getElementById: function (id) { return nodes[id] || null; } };
var box = new El("div"); nodes["sb-panel-progress"] = box;
var PlanUI = require(process.argv[2]);
var P = JSON.parse(process.argv[3]);
global.PlanPage = { state: { snapshot: { generated: "2026-10-09T00:00:00Z", progress: P } } };
PlanUI.ProgressPage.render();
var rows = PlanUI.ProgressPage.rows(P);
global.PlanPage = { state: { snapshot: {} } };
var empty = new El("div"); nodes["sb-panel-progress"] = empty;
PlanUI.ProgressPage.render();
process.stdout.write(JSON.stringify({ text: box.textContent, rows: rows.map(function (r) {
  return [r.project, r.pct, r.done, r.total, r.active, r.operator]; }), empty: empty.textContent,
  fill: box.children[1].children[0].children[1].children[0].children[1].children[0].children[0].style.width }));
"""


@need_node
def test_progress_page_renders_rows_and_running(tmp_path):
    rp = tmp_path / "p.js"
    rp.write_text(PAGE_JS, encoding="utf-8")
    data = json.dumps(progress(ITEMS))
    proc = subprocess.run(["node", str(rp), str(WEB / "page_progress.js"), data],
                          capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert out["rows"] == [["p", 25, 1, 4, 2, 1], ["q", 100, 1, 1, 0, 0]]
    assert "All projects: 40% done (2/5), 6 h left. As of 2026-10-09T00:00:00Z." in out["text"]
    assert "model muse-spark" in out["text"] and "model unknown" in out["text"]
    assert out["fill"] == "25%"
    assert out["empty"] == "No progress data in this snapshot."
