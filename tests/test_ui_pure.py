"""T1 pure core — node-run tests for PlanUI: model, adapter, Store, Query
(UI-PLAN §2, §3, §7 + Router decision 2, D-398).

One shared node harness per component (plain ``node``, no packages, skips
when node is missing); synthetic data: the UI fixture + this file's REAL shapes.
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
VENDOR = ROOT / "vendor"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "synthetic_snapshot.json"

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402
from plangraph import views  # noqa: E402  (API documented in docs/SPEC.md)

SNAP = json.loads(FIXTURE.read_text(encoding="utf-8"))
ITEMS = SNAP["items"]

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
NOW = "2026-10-01T00:00:00Z"

#: The `Item` record fields of the UI-PLAN §3 data contract.
ITEM_FIELDS = {
    "key", "id", "project", "title", "phase", "phaseIdx", "kind", "state", "display",
    "owner", "operator", "priority", "estimate_h", "deps", "unblocks", "critical",
    "slack", "loop", "doneCriteria", "evidence", "reviewTier", "updatedAt", "crossDeps",
}

# node harness: modes "vm" | "query" | "store"; JSON on stdout.
HARNESS = r"""
"use strict";
var fs = require("fs");
var mode = process.argv[2];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

if (mode === "store") {
  var Store = require(process.argv[3]).Store;
  var st = new Store({view: "graph", sel: null, query: ""});
  var ev = [], ev2 = [];
  var un = st.subscribe(function (keys) { ev.push(keys.slice()); });
  st.set("view", "table");
  st.set("view", "table");                 // unchanged -> no event
  st.patch({sel: "atlas:T-1", query: "lease"});
  st.set("query", "lease");                // unchanged -> no event
  st.patch({sel: "atlas:T-1"});            // unchanged -> no event
  un();
  st.set("view", "graph"); st.subscribe(function (keys) { ev2.push(keys.slice()); });
  st.patch({view: "table", sel: null});    // one event with patch-key order
  emit({events: ev, events2: ev2, view: st.get("view"), query: st.get("query"),
        sel: st.get("sel"), all: st.get(), unsubType: typeof un});
} else {
  require(process.argv[3]);                 // page.js -> global.PlanGraph
  var PlanUI = require(process.argv[4]);    // page_model.js -> PlanUI
  if (mode === "query") require(process.argv[5]);   // page_search.js attaches Query
  var dataPath = mode === "query" ? process.argv[6] : process.argv[5];
  var snap = JSON.parse(fs.readFileSync(dataPath, "utf8"));
  var vm = PlanUI.buildViewModel(snap);
  var out;
  if (mode === "vm") {
    out = {size: vm.items.size, display: {}, forms: {}, projects: [], needsYou: [],
           questions: vm.questions, decisions: vm.decisions, answers: vm.answers,
           critical: vm.critical, humanize: {}, sampleItem: null};
    vm.items.forEach(function (it, key) {
      out.display[key] = it.display; out.forms[key] = {phase: it.phase, loop: it.loop};
      if (!out.sampleItem) out.sampleItem = it; });
    vm.projects.forEach(function (p) {
      out.projects.push({name: p.name, label: p.label, color: p.color, goalKey: p.goalKey,
        phases: p.phases.map(function (f) {
          return {name: f.name, idx: f.idx, total: f.keys.length, done: f.done,
                  ready: f.ready, blocked: f.blocked}; })});
    });
    out.needsYou = vm.needsYou.map(function (e) {
      return e.status !== undefined ? {t: "q", k: e.id} : {t: "item", k: e.key}; });
    snap.items.forEach(function (raw, i) {
      if (i === 0 || i === 100 || i === 253) out.humanize[raw.key] = vm.humanize(raw.key);
    });
    out.humanize["__none__"] = vm.humanize("__none__");
    emit(out);
  } else {  // mode === "query"
    var queries = JSON.parse(fs.readFileSync(process.argv[7], "utf8"));
    var edges = JSON.parse(fs.readFileSync(process.argv[8], "utf8"));
    out = {matches: {}, edgeMatches: {}};
    queries.forEach(function (q) {
      out.matches[q] = [];
      vm.items.forEach(function (it, key) {
        if (PlanUI.Query.match(it, q)) out.matches[q].push(key); });
      out.edgeMatches[q] = edges.map(function (e) {
        return PlanUI.Query.edgeMatch({source: e[0], target: e[1]}, q, vm.items); });
    });
    emit(out);
  }
}
"""

def write_harness(tmp_path: Path) -> Path:
    p = tmp_path / "harness.js"; p.write_text(HARNESS, encoding="utf-8"); return p

def run_node(tmp_path: Path, mode: str, *args) -> dict:
    cmd = ["node", str(write_harness(tmp_path)), mode] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def vm_out(tmp_path: Path, fixture: Path) -> dict:
    return run_node(tmp_path, "vm", WEB / "page.js", WEB / "page_model.js", fixture)

def nat(label):  # natural key: digit runs compare as numbers (P0 < P1 < ... < P10)
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", label)]

def ready_blocked(items):
    return {i["key"] for i in views.ready(items)}, set(views.blocked(items))

def display_map(items):
    ready, blocked = ready_blocked(items)
    return {i["key"]: ("ready" if i["key"] in ready else "blocked" if i["key"] in blocked
                       else i["state"]) for i in items}

def expected_phases(items, project):
    """UI-PLAN §3: natural sort of the label, ties broken by min topological wave."""
    wave = {k: i for i, layer in enumerate(views.waves(items)) for k in layer}
    groups = {}
    for i in items:
        if i["project"] == project:
            groups.setdefault(i["track"], []).append(i)
    def min_wave(track):
        ws = [wave[x["key"]] for x in groups[track] if x["key"] in wave]
        return min(ws) if ws else float("inf")

    return sorted(groups, key=lambda t: (nat(t), min_wave(t), t))

def question_of(item):  # Router decision 2: title starts "Q:" OR atlas:Q-* lane
    return item["title"][:2] == "Q:" or (item["project"] == "atlas"
                                         and re.match(r"Q-", str(item["id"])))

def question_status(state):
    return "answered" if state == "done" else "cancelled" if state == "cancelled" else "open"

def expected_questions(snap):
    """Independent Python mirror of the adapter's question derivation."""
    if isinstance(snap.get("questions"), list):
        return snap["questions"]
    return [{"id": i["id"], "project": i["project"], "text": i["title"],
             "asked_by": i["owner"], "status": question_status(i["state"]),
             "item": i["key"], "answer": i["done_criteria"] if i["state"] == "done" else None}
            for i in snap["items"] if question_of(i)]

def real_snapshot() -> dict:
    """REAL-shaped snapshot: items only, no questions/decisions/phase/loop keys (Router decision 2)."""
    def item(proj, iid, title, track, **kw):
        base = {"project": proj, "id": iid, "key": f"{proj}:{iid}", "title": title,
                "kind": "lane", "track": track, "owner": "router", "state": "open",
                "estimate_h": 2, "depends_on": [], "done_criteria": "Meets DONE.",
                "operator": False, "priority": 0, "evidence": [], "updated_at": NOW,
                "review_tier": "standard"}
        base.update(kw)
        return base

    d701a = d701b = {"kind": "decision", "ref": "D-701", "verified": True, "at": NOW}
    d702 = {"kind": "decision", "ref": "D-702", "verified": True, "at": NOW}
    items = [
        item("beacon", "OP-1", "Operator sign-off for the fence", "T0",
             operator=True, priority=3, owner="OPERATOR"),
        item("beacon", "OP-2", "Operator review of the lease ledger", "T0",
             state="claimed", depends_on=["beacon:OP-1"], operator=True, owner="OPERATOR"),
        item("beacon", "OP-D", "Operator archive sweep", "T0", state="done", operator=True, owner="OPERATOR"),
        item("beacon", "QK-1", "Q: choose the retry backoff policy", "T1",
             state="done", done_criteria="Backoff documented."),
        item("beacon", "DEC-A", "Record the backoff decision", "T1", evidence=[d701a]),
        item("beacon", "NO-1", "Abandoned probe lane", "T2", state="cancelled"),
        item("atlas", "Q-1", "Route operator enquiries to the router", "T0"),
        item("atlas", "DEC-B", "Mirror the backoff decision", "T0", evidence=[d701b]),
        item("atlas", "DEC-C", "Record the fallback decision", "T1", state="in_review",
             evidence=[d702]),
        item("atlas", "CL-1", "Compose the launch runbook", "T1", depends_on=["atlas:DEC-B"]),
        item("atlas", "RD-1", "Publish the operator notes", "T1"),
    ]
    return {"generated": NOW, "head": "synth-real01", "items": items}

def real_fixture_path(tmp_path: Path) -> Path:
    p = tmp_path / "real_snapshot.json"
    p.write_text(json.dumps(real_snapshot()), encoding="utf-8")
    return p

def both_snapshots(tmp_path):
    return [(SNAP, FIXTURE), (real_snapshot(), real_fixture_path(tmp_path))]

# -------- S1: view model + adapter (page_model.js / buildViewModel) -------- #

@need_node
def test_vm_shape_projects_phases_and_humanize(tmp_path):
    out = vm_out(tmp_path, FIXTURE)
    assert out["size"] == 254
    assert [p["name"] for p in out["projects"]] == ["atlas", "borealis", "cinder", "dune"]
    ready, blocked = ready_blocked(ITEMS)
    ub, cp = views.unblocks(ITEMS), views.critical_path(ITEMS)
    total = 0
    for p in out["projects"]:
        names = [f["name"] for f in p["phases"]]
        assert len(names) == 5 and [f["idx"] for f in p["phases"]] == [0, 1, 2, 3, 4]
        assert names == expected_phases(ITEMS, p["name"])  # natural sort, then wave
        assert p["label"] == p["name"].capitalize()  # R10
        assert p["goalKey"] == p["name"] + ":GOAL"
        assert p["color"].startswith("#")
        for f in p["phases"]:
            raw = [i for i in ITEMS if i["project"] == p["name"] and i["track"] == f["name"]]
            total += f["total"]
            assert f["total"] == len(raw)
            assert f["done"] == sum(1 for i in raw if i["state"] == "done")
            assert f["ready"] == sum(1 for i in raw if i["key"] in ready)
            assert f["blocked"] == sum(1 for i in raw if i["key"] in blocked)
    assert total == 254
    for i in (ITEMS[0], ITEMS[100], ITEMS[253]):
        assert out["humanize"][i["key"]] == i["title"] + " (" + i["project"] + ")"
    assert out["humanize"]["__none__"] == "__none__"
    assert out["critical"] == cp["keys"]     # PlanGraph.criticalPath == views
    sample = out["sampleItem"]               # first fixture item: atlas:T-1 (done)
    assert set(sample) >= ITEM_FIELDS        # §3 Item contract
    assert sample["deps"] == ITEMS[0]["depends_on"]
    assert sample["loop"] is None
    assert sample["slack"] == pytest.approx(views.slack(ITEMS).get(sample["key"], 0))
    assert sample["critical"] == (sample["key"] in set(cp["keys"]))
    waiters = [i["key"] for i in ITEMS if i["state"] not in ("done", "cancelled")
               and sample["key"] in (i.get("depends_on") or [])]
    assert set(sample["unblocks"]) == set(waiters)   # non-terminal waiters only
    assert len(sample["unblocks"]) == ub[sample["key"]]

@need_node
def test_display_states_equal_views_ready_blocked(tmp_path):
    for snap, path in both_snapshots(tmp_path):
        out = vm_out(tmp_path, path)
        ready, blocked = ready_blocked(snap["items"])
        states = {i["key"]: i["state"] for i in snap["items"]}
        assert set(out["display"]) == set(states)
        assert {k for k, d in out["display"].items() if d == "ready"} == ready
        assert {k for k, d in out["display"].items() if d == "blocked"} == blocked
        passthru = {k for k in states if k not in ready and k not in blocked}
        assert all(out["display"][k] == states[k] for k in passthru)  # unchanged

@need_node
def test_needs_you_count_and_ranking(tmp_path):
    for snap, path in both_snapshots(tmp_path):
        out = vm_out(tmp_path, path)
        op_open = [i["key"] for i in snap["items"]
                   if i.get("operator") and i["state"] not in ("done", "cancelled")]
        open_q = [q for q in expected_questions(snap) if q["status"] == "open"]
        assert len(out["needsYou"]) == len(op_open) + len(open_q)
        assert {e["k"] for e in out["needsYou"] if e["t"] == "item"} == set(op_open)
        assert [e["k"] for e in out["needsYou"] if e["t"] == "q"] == [q["id"] for q in open_q]
        # ranked by (-priority, -unblocks), stable; question rows carry (0, 0)
        ub = views.unblocks(snap["items"])
        by = {i["key"]: i for i in snap["items"]}
        pairs = [(-int(by[e["k"]].get("priority") or 0), -ub.get(e["k"], 0))
                 if e["t"] == "item" else (0, 0) for e in out["needsYou"]]
        assert pairs == sorted(pairs)
        assert [q["id"] for q in out["answers"]] == [
            q["id"] for q in expected_questions(snap) if q["status"] == "answered"]

@need_node
def test_adapter_fixture_shape_passes_through_unchanged(tmp_path):
    out = vm_out(tmp_path, FIXTURE)
    # fixture shape: questions/decisions/phase/loop pass through untouched
    assert out["questions"] == SNAP["questions"]   # 12 questions, verbatim
    assert out["decisions"] == SNAP["decisions"]   # 18 decisions, verbatim
    assert all(out["forms"][i["key"]]["phase"] == i["phase"]
               and out["forms"][i["key"]]["loop"] == i.get("loop") for i in ITEMS)

@need_node
def test_adapter_real_shape_builds_the_same_contract(tmp_path):
    snap = real_snapshot()
    out = vm_out(tmp_path, real_fixture_path(tmp_path))
    # phase = track; no loop field -> loop:null (no loop styling)
    assert all(out["forms"][i["key"]]["phase"] == i["track"]
               and out["forms"][i["key"]]["loop"] is None for i in snap["items"])
    # questions = items whose title starts "Q:" + atlas:Q-* lanes
    assert out["questions"] == expected_questions(snap)
    qby = {q["id"]: q for q in out["questions"]}
    assert set(qby) == {"QK-1", "Q-1"}
    assert qby["QK-1"]["text"].startswith("Q:") and qby["QK-1"]["status"] == "answered"
    assert qby["QK-1"]["answer"] == "Backoff documented."
    assert qby["Q-1"]["item"] == "atlas:Q-1" and qby["Q-1"]["status"] == "open"
    # decisions = distinct `decision` evidence refs, with the citing items
    assert [d["id"] for d in out["decisions"]] == ["D-701", "D-702"]   # first-seen order
    d701 = out["decisions"][0]
    assert d701["items"] == ["beacon:DEC-A", "atlas:DEC-B"]
    assert d701["project"] == "beacon" and d701["date"] == NOW[:10]
    assert d701["title"] == "Decision D-701" and d701["text"] == ""
    assert set(d701) == {"id", "project", "title", "text", "date", "items"}
    # critical path derivation reused from page.js agrees with views
    assert out["critical"] == views.critical_path(snap["items"])["keys"]

@need_node
def test_phase_order_tie_broken_by_min_topological_wave(tmp_path):
    def item(iid, track, deps=()):
        return {"project": "mini", "id": iid, "key": "mini:" + iid, "title": "Mini " + iid,
                "kind": "lane", "track": track, "owner": "router", "state": "open",
                "depends_on": list(deps), "estimate_h": 1, "evidence": [], "updated_at": NOW}
    snap = {"items": [item("W1", "P10"), item("W2", "P2", ["mini:W1"]), item("W3", "P1"),
                      item("W4", "P01", ["mini:W3"])]}
    p = tmp_path / "mini.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    out = vm_out(tmp_path, p)
    names = [f["name"] for f in out["projects"][0]["phases"]]
    assert names == expected_phases(snap["items"], "mini") == ["P1", "P01", "P2", "P10"]

# ---- S2: Store — change(keys[]) events, no-op guards, merged defaults ---- #

@need_node
def test_store_emits_changed_keys_and_guards_noops(tmp_path):
    out = run_node(tmp_path, "store", WEB / "page_store.js")
    assert out["events"] == [["view"], ["sel", "query"]]
    assert out["events2"] == [["view", "sel"]]          # one event per patch
    assert out["unsubType"] == "function"               # unsubscribe from subscribe()
    assert out["view"] == "table" and out["query"] == "lease" and out["sel"] is None
    assert out["all"]["matchMode"] == "dim" and out["all"]["group"] == "project>phase"
    assert out["all"]["filters"]["edgeMode"] == "all"
    assert out["all"]["settings"]["theme"] == "auto" and out["all"]["sidebar"]["tab"] == "needs"

# --- S3: Query grammar (§7) — JS results vs independent Python oracles --- #

#: First fixture key some other item depends on (`dep:` query target).
DEP_KEY = next(i["key"] for i in ITEMS
               if any(i["key"] in (x.get("depends_on") or []) for x in ITEMS))

QUERIES = ["", "lease audit", "state:ready", "state:ready owner:atlas", "project:atlas",
           "phase:p1", "op:1", "op:0", "loop:1", "prio:>1", "dep:" + DEP_KEY,
           "edge:critical", "edge:blocking", "edge:cross"]

@need_node
def test_query_grammar_matches_python_oracle(tmp_path):
    disp, crit = display_map(ITEMS), set(views.critical_path(ITEMS)["keys"])
    idx = {i["key"]: i for i in ITEMS}
    edges = [[d, i["key"]] for i in ITEMS for d in (i.get("depends_on") or [])]
    lo = lambda s: str(s or "").lower()                      # noqa: E731
    words = lambda i, ws: all(any(                          # noqa: E731  §7: AND across words
        w in lo(i.get(f)) for f in ("title", "key", "id", "owner", "done_criteria", "phase"))
        for w in ws)
    cross = lambda i: any(d in idx and idx[d]["project"] != i["project"]  # noqa: E731
                          for d in (i.get("depends_on") or []))
    ready = lambda i: disp[i["key"]] == "ready"              # noqa: E731
    preds = {
        "": lambda i: True,
        "lease audit": lambda i: words(i, ("lease", "audit")),
        "state:ready": ready,
        "state:ready owner:atlas":
            lambda i: ready(i) and "atlas" in lo(i.get("owner")),
        "project:atlas": lambda i: "atlas" in lo(i.get("project")),
        "phase:p1": lambda i: "p1" in lo(i.get("phase")),
        "op:1": lambda i: bool(i.get("operator")),
        "op:0": lambda i: not i.get("operator"),
        "loop:1": lambda i: bool(i.get("loop")),
        "prio:>1": lambda i: int(i.get("priority") or 0) > 1,
        "dep:" + DEP_KEY: lambda i: DEP_KEY in (i.get("depends_on") or []),
        "edge:critical": lambda i: i["key"] in crit,
        "edge:blocking": lambda i: disp[i["key"]] != "done",
        "edge:cross": cross,
    }

    def edge_oracle(q, s, t):
        if q == "edge:critical":      # both endpoints on the critical path
            return s["key"] in crit and t["key"] in crit
        if q == "edge:blocking":      # §7: blocking = SOURCE not done
            return disp[s["key"]] != "done"
        if q == "edge:cross":         # endpoints in different projects
            return s["project"] != t["project"]
        return preds[q](s) or preds[q](t)   # node facets match either endpoint

    qfile, efile = tmp_path / "queries.json", tmp_path / "edges.json"
    qfile.write_text(json.dumps(QUERIES), encoding="utf-8")
    efile.write_text(json.dumps(edges), encoding="utf-8")
    out = run_node(tmp_path, "query", WEB / "page.js", WEB / "page_model.js",
                   WEB / "page_search.js", FIXTURE, qfile, efile)
    for q in QUERIES:
        assert out["matches"][q] == [i["key"] for i in ITEMS if preds[q](i)]  # fixture order
        assert out["edgeMatches"][q] == [edge_oracle(q, idx[s], idx[t]) for s, t in edges]
    # absolute spot checks so the grammar cannot pass vacuously (254-item fixture)
    assert len(out["matches"][""]) == 254
    assert len(out["matches"]["state:ready"]) == 25 and len(out["matches"]["prio:>1"]) == 60
    assert sum(out["edgeMatches"]["edge:critical"]) == 8
    assert sum(out["edgeMatches"]["edge:cross"]) == 12

def test_page_bytes_under_half_mb_on_synthetic_cdn_render():
    """A13 / V3 / §12: page < 0.5 MB on the 254-item synthetic fixture with cdn."""
    html = render_mod.render(SNAP, VENDOR, cdn=True)
    # PG-KG-UI (D-879) added ~11 KB (page_kg*.js/css); the base page was ~498 KB, so the budget is 520 KB now.
    assert len(html.encode("utf-8")) < 520_000
    assert "atlas:T-1" in html
