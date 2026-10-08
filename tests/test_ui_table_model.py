"""GU-TB1 table model tests, part 1: constants, context, defaults/normalize,
filter/search, sort (T1-T5). Node-run, skipped without node; every expected
value is computed in Python from the contract text or by an independent oracle
(plain loops over the runner's record dump), never read back from the
implementation.
"""
from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ui_table_util import RUNNER_BOOT, need_node, run_node, vm_dump  # noqa: E402

RUNNER = RUNNER_BOOT + r"""
if (mode === "t1") {
  emit({ states: PlanUI.TABLE_STATES, columns: PlanUI.TABLE_COLUMNS, metrics: PlanUI.TABLE_METRICS });
} else if (mode === "t2") {
  var vm = vmOf(P.fix), ctx = PlanUI.tableContext(vm);
  var ident = true, n = 0;
  vm.items.forEach(function (r) { if (ctx.rows[n++] !== r) ident = false; });
  var bOk = true, bSize = 0;
  for (var k in ctx.byKey) { bSize++; if (ctx.byKey[k] !== vm.items.get(k)) bOk = false; }
  var bAll = true;
  vm.items.forEach(function (r, kk) { if (!ctx.byKey[kk]) bAll = false; });
  var ctxB = PlanUI.tableContext(vmOf(IN.bigPath));
  var ctxF = PlanUI.tableContext(freezeVm(vm));
  emit({ ident: ident, byKeyIdent: bOk, byKeySize: bSize, byKeyComplete: bAll,
         total: ctx.total, projects: ctx.projects,
         projIdx: ctx.projIdx, owners: ctx.owners, phases: ctx.phases, kinds: ctx.kinds,
         priorities: ctx.priorities, totalBig: ctxB.total, totalFrozen: ctxF.total,
         frozenOwners: ctxF.owners,
         empty: [PlanUI.tableContext({}).total, PlanUI.tableContext(null).total,
                 PlanUI.tableContext({ items: [] }).total] });
} else if (mode === "t3") {
  var outs = IN.cases.map(function (c) { return PlanUI.tableNormalize(c); });
  var idem = IN.twice.map(function (x) {
    var a = PlanUI.tableNormalize(x);
    return JSON.stringify(a) === JSON.stringify(PlanUI.tableNormalize(a));
  });
  var d1 = PlanUI.tableDefaults(), d2 = PlanUI.tableDefaults();
  var eq = JSON.stringify(d1) === JSON.stringify(d2);
  d1.filters.project.push("zzz"); d1.sort[0].col = "title";
  emit({ outs: outs, echo: IN.cases, idem: idem, defEq: eq,
         def2Clean: d2.filters.project.length === 0 && d2.sort[0].col === "priority" });
} else if (mode === "t4") {
  var ctx4 = PlanUI.tableContext(vmOf(IN.bigPath || P.fix));
  ctx4.rows.forEach(function (r) { deepFreeze(r); });
  emit({ results: IN.states.map(function (st) {
    deepFreeze(st);
    return PlanUI.tableFilter(ctx4, st).map(function (r) { return r.key; });
  }) });
} else if (mode === "t5") {
  var ctx5 = PlanUI.tableContext(vmOf(IN.bigPath || P.fix));
  var rows = ctx5.rows.slice();
  ctx5.rows.forEach(function (r) { deepFreeze(r); });
  emit({ results: IN.specs.map(function (spec) {
    var sorted = PlanUI.tableSort(rows, { sort: spec, group: "none" }, ctx5);
    var again = PlanUI.tableSort(sorted, { sort: spec, group: "none" }, ctx5);
    return { keys: sorted.map(function (r) { return r.key; }),
             again: again.map(function (r) { return r.key; }),
             fresh: Array.isArray(sorted) && sorted !== rows,
             inputAfter: rows.map(function (r) { return r.key; }) };
  }) });
} else if (mode === "t5odd") {
  var vmO = { projects: [{ name: "a", label: "A", color: "#111111" }],
    items: [
      { key: "a:1", project: "a", title: "R", phase: "P1", phaseIdx: 0, kind: "lane",
        display: "ready", owner: "o", operator: false, priority: 1, estimate_h: 1, deps: [], unblocks: [] },
      { key: "a:2", project: "a", title: "W", phase: "P1", phaseIdx: 0, kind: "lane",
        display: "weird", owner: "o", operator: false, priority: 1, estimate_h: 1, deps: [], unblocks: [] },
      { key: "a:3", project: "a", title: "B", phase: "P1", phaseIdx: 0, kind: "lane",
        display: "blocked", owner: "o", operator: false, priority: 1, estimate_h: 1, deps: [], unblocks: [] }
    ] };
  var ctxO = PlanUI.tableContext(vmO);
  var sortedO = PlanUI.tableSort(ctxO.rows, { sort: [{ col: "state", dir: "asc" }] }, ctxO);
  emit({ keys: sortedO.map(function (r) { return r.key; }) });
} else if (mode === "t2none") {
  var vmN = { projects: [{ name: "a", label: "A", color: "#111111" }],
    items: [
      { key: "a:1", project: "a", title: "One", phase: "P1", phaseIdx: 0, kind: "lane",
        display: "ready", owner: "o", operator: false, priority: 1, estimate_h: 1, deps: [], unblocks: [] },
      null,
      { key: "g:1", project: "ghost", title: "Ghost", phase: "P1", phaseIdx: 0, kind: "lane",
        display: "open", owner: "o", operator: false, priority: 1, estimate_h: 1, deps: [], unblocks: [] }
    ] };
  var ctxN = PlanUI.tableContext(vmN), stN = PlanUI.tableDefaults();
  var appN = PlanUI.tableApply(ctxN, stN, {});
  emit({ total: ctxN.total, projects: ctxN.projects, projIdx: ctxN.projIdx,
         byKeySize: Object.keys(ctxN.byKey).length,
         filtered: PlanUI.tableFilter(ctxN, stN).map(function (r) { return r.key; }),
         groupedRowKeys: appN.rows.map(function (r) { return r.key; }),
         entryTags: appN.entries.map(function (e) {
           return e.type === "group" ? "group:" + e.id : e.type + ":" + e.row.key;
         }) });
}
"""

TABLE_STATES = ["ready", "blocked", "claimed", "in_review", "open", "done", "cancelled"]
TABLE_COLUMNS = [
    ("state", "State", 112, False, "asc", True), ("project", "Project", 120, False, "asc", True),
    ("title", "Title", 420, False, "asc", True), ("phase", "Phase", 150, False, "asc", True),
    ("owner", "Owner", 130, False, "asc", True), ("priority", "Prio", 64, True, "desc", True),
    ("est", "Est h", 64, True, "desc", True), ("kind", "Kind", 80, False, "asc", False),
    ("op", "Op", 56, False, "desc", True), ("unblocks", "Unblocks", 88, True, "desc", True),
]
TABLE_METRICS = {"rowH": 36, "groupH": 32, "detailH": 128, "overscan": 6}
DEFAULT_COLS = ["state", "project", "title", "phase", "owner", "priority", "est", "op", "unblocks"]


def defaults():
    """C5 tableDefaults, written out from the contract."""
    return {"sort": [{"col": "priority", "dir": "desc"}],
            "filters": {"project": [], "phase": [], "state": [], "owner": [],
                        "priority": [], "kind": [], "op": False},
            "search": "", "cols": list(DEFAULT_COLS), "group": "project>phase", "collapsed": []}


FACET_NAMES = {"state", "owner", "project", "phase", "op", "loop", "prio", "dep", "edge"}
WORD_FIELDS = ["title", "key", "id", "owner", "doneCriteria", "phase"]
STATE_IDX = {s: i for i, s in enumerate(TABLE_STATES)}


def query_match(rec, q):
    """Independent oracle for PlanUI.Query.match (node grammar, plain loops)."""
    for tok in str(q).split():
        ci = tok.find(":")
        name = tok[:ci].lower() if ci > 0 else ""
        arg = tok[ci + 1:]
        if name not in FACET_NAMES:
            w = tok.lower()
            if not any(w in str(rec.get(f) or "").lower() for f in WORD_FIELDS):
                return False
            continue
        if name == "state":
            if rec["display"] != arg:
                return False
        elif name in ("owner", "project", "phase"):
            if arg.lower() not in str(rec.get(name) or "").lower():
                return False
        elif name == "op":
            if (arg == "1") != bool(rec["operator"]):
                return False
        elif name == "loop":
            if (arg == "1") != bool(rec["loop"]):
                return False
        elif name == "prio":
            m = re.fullmatch(r"([<>]=?|=)?(-?\d+)", arg.lower())
            if not m:
                return False
            v = rec["priority"] if rec["priority"] is not None else 0
            nv = int(m.group(2))
            opn = m.group(1) or "="
            ok = {">": v > nv, "<": v < nv, ">=": v >= nv, "<=": v <= nv}.get(opn, v == nv)
            if not ok:
                return False
        elif name == "dep":
            if arg not in rec["deps"]:
                return False
        elif name == "edge":
            ok = (rec["critical"] if arg == "critical"
                  else rec["display"] != "done" if arg == "blocking"
                  else len(rec["crossDeps"]) > 0 if arg == "cross" else False)
            if not ok:
                return False
    return True


def contract_filter(records, st):
    """Independent oracle for tableFilter (C6), plain loops over records."""
    f = st.get("filters") or {}
    search = str(st.get("search") or "").strip()
    out = []
    for rec in records:
        if f.get("project") and (rec["project"] or "") not in f["project"]:
            continue
        if f.get("phase") and (rec["phase"] or "") not in f["phase"]:
            continue
        if f.get("state") and rec["display"] not in f["state"]:
            continue
        if f.get("owner") and rec["owner"] not in f["owner"]:
            continue
        if f.get("priority") and (rec["priority"] or 0) not in f["priority"]:
            continue
        if f.get("kind") and rec["kind"] not in f["kind"]:
            continue
        if f.get("op") and not (rec["operator"] and rec["display"] not in ("done", "cancelled")):
            continue
        if search and not query_match(rec, search):
            continue
        out.append(rec["key"])
    return out


def natural_key(s):
    """Independent natural-compare key (chunk-based, digits numeric-first,
    text lower-cased) matching the C7 phase comparator semantics."""
    return [(0, int(c)) if c.isdigit() else (1, c.lower())
            for c in re.split(r"(\d+)", s or "") if c != ""]


def one_cmp(col, desc, a, b, proj_idx):
    """C7 comparator for one column; desc negates only this key."""
    if col == "state":
        d = STATE_IDX.get(a["display"], 99) - STATE_IDX.get(b["display"], 99)
    elif col == "project":
        d = proj_idx.get(a["project"] or "", 9999) - proj_idx.get(b["project"] or "", 9999)
    elif col == "phase":
        ka, kb = natural_key(a["phase"] or ""), natural_key(b["phase"] or "")
        d = ((ka > kb) - (ka < kb)) or (proj_idx.get(a["project"] or "", 9999)
                                        - proj_idx.get(b["project"] or "", 9999))
    elif col in ("title", "owner", "kind"):
        x, y = str(a[col] or "").lower(), str(b[col] or "").lower()
        d = (x > y) - (x < y)
    elif col == "priority":
        d = (a["priority"] or 0) - (b["priority"] or 0)
    elif col == "est":
        d = (a["estimate_h"] or 0) - (b["estimate_h"] or 0)
    elif col == "unblocks":
        d = len(a["unblocks"]) - len(b["unblocks"])
    else:  # op or unknown column
        need_a = a["operator"] and a["display"] not in ("done", "cancelled")
        need_b = b["operator"] and b["display"] not in ("done", "cancelled")
        d = (1 if need_a else 0) - (1 if need_b else 0) if col == "op" else 0
    return -d if desc == "desc" else d


def contract_sort(records, spec, proj_idx):
    """Independent oracle for tableSort: decorate with the ctx index, compare,
    tie-break on the ctx index (plain cmp, different style from the JS)."""
    order = {r["key"]: i for i, r in enumerate(records)}

    def cmp(x, y):
        for s in spec:
            d = one_cmp(s["col"], s.get("dir", "asc"), x, y, proj_idx)
            if d:
                return d
        return order[x["key"]] - order[y["key"]]

    return [r["key"] for r in sorted(records, key=functools.cmp_to_key(cmp))]


@pytest.fixture(scope="module")
def dump(tmp_path_factory):
    d, big = vm_dump(tmp_path_factory.mktemp("tb1dump"))
    return {"dump": d, "big": str(big)}


@need_node
def test_t1_constants(dump, tmp_path):
    res = run_node(tmp_path, RUNNER, "t1")
    assert res["states"] == TABLE_STATES
    got = [(c["id"], c["label"], c["w"], c["num"], c["dir"], c["on"]) for c in res["columns"]]
    assert got == TABLE_COLUMNS
    assert len({c["id"] for c in res["columns"]}) == len(res["columns"])
    assert res["metrics"] == TABLE_METRICS


@need_node
def test_t2_context(dump, tmp_path):
    res = run_node(tmp_path, RUNNER, "t2", {"bigPath": dump["big"]})
    recs, projs = dump["dump"]["records"], dump["dump"]["projects"]
    assert res["ident"] and res["byKeyIdent"]
    assert res["byKeySize"] == len(recs) and res["byKeyComplete"]
    assert res["total"] == len(recs) and res["totalBig"] == 1200
    assert res["projects"] == [{"name": p["name"], "label": p["label"], "color": p["color"]}
                               for p in projs]
    assert res["projIdx"] == {p["name"]: i for i, p in enumerate(projs)}
    assert res["owners"] == sorted({r["owner"] for r in recs if r["owner"]})
    assert res["phases"] == sorted({r["phase"] for r in recs if r["phase"]})
    assert res["kinds"] == sorted({r["kind"] for r in recs if r["kind"]})
    assert res["priorities"] == sorted({int(r["priority"] or 0) for r in recs
                                        if r["priority"] is not None})
    assert res["totalFrozen"] == res["total"] and res["frozenOwners"] == res["owners"]
    assert res["empty"] == [0, 0, 0]


@need_node
def test_t5odd_unknown_display_sorts_last(dump, tmp_path):
    """C1/C7: a display value outside TABLE_STATES sorts after all known ones."""
    res = run_node(tmp_path, RUNNER, "t5odd")
    assert res["keys"] == ["a:1", "a:3", "a:2"]     # ready(0), blocked(1), weird(99) last


@need_node
def test_t2none_ghost_and_falsy_items(dump, tmp_path):
    """C4/C8: rows with an unknown project fold into the synthetic "" (none)
    project (grouped entries included); falsy vm items are skipped, no throw."""
    res = run_node(tmp_path, RUNNER, "t2none")
    assert res["total"] == 2 and res["byKeySize"] == 2
    assert res["projects"] == [{"name": "a", "label": "A", "color": "#111111"},
                               {"name": "", "label": "(none)", "color": "#2f6f4f"}]
    assert res["projIdx"] == {"a": 0, "": 1}
    assert res["filtered"] == ["a:1", "g:1"]
    assert res["groupedRowKeys"] == ["a:1", "g:1"]
    assert res["entryTags"] == ["group:p:a", "group:ph:a|P1", "row:a:1",
                                "group:p:", "group:ph:|P1", "row:g:1"]


@need_node
def test_t3_defaults_and_normalize(dump, tmp_path):
    d = defaults()
    cases = [
        (None, d), (42, d), ({}, d),
        ({"sort": [{"col": "bogus", "dir": "asc"}]}, {**d, "sort": d["sort"]}),
        ({"sort": [{"col": "title", "dir": "sideways"}]}, {**d, "sort": [{"col": "title", "dir": "asc"}]}),
        ({"sort": [{"col": "est", "dir": "desc"}, {"col": "est", "dir": "asc"}]},
         {**d, "sort": [{"col": "est", "dir": "desc"}]}),
        ({"sort": [{"col": "title", "dir": "asc"}, {"col": "est", "dir": "desc"},
                   {"col": "priority", "dir": "asc"}, {"col": "owner", "dir": "asc"},
                   {"col": "kind", "dir": "desc"}, {"col": "state", "dir": "asc"}]},
         {**d, "sort": [{"col": "title", "dir": "asc"}, {"col": "est", "dir": "desc"},
                        {"col": "priority", "dir": "asc"}, {"col": "owner", "dir": "asc"}]}),
        ({"filters": {"project": ["b", "a", "b"], "priority": ["3", "1", "10"]}},
         {**d, "filters": {**d["filters"], "project": ["a", "b"], "priority": [1, 3, 10]}}),
        ({"cols": ["est", "state"]}, {**d, "cols": ["state", "title", "est"]}),
        ({"cols": []}, {**d, "cols": ["title"]}),
        ({"cols": ["kind"]}, {**d, "cols": ["title", "kind"]}),
        ({"cols": ["bogus", "title"]}, {**d, "cols": ["title"]}),
        ({"group": "weird"}, d), ({"group": "none"}, {**d, "group": "none"}),
        ({"filters": {"op": "yes"}}, {**d, "filters": {**d["filters"], "op": True}}),
        ({"search": 5}, {**d, "search": "5"}),
        ({"sort": "x"}, d),
        ({"collapsed": ["b", "a", "b"]}, {**d, "collapsed": ["a", "b"]}),
        ({"filters": "x"}, d),
    ]
    res = run_node(tmp_path, RUNNER, "t3", {"cases": [c for c, _ in cases],
                                            "twice": [cases[7][0], {}]})
    assert res["defEq"] and res["def2Clean"]
    assert json.loads(json.dumps(res["echo"])) == json.loads(json.dumps([c for c, _ in cases]))
    for got, (_, want) in zip(res["outs"], cases):
        assert got == want
    assert all(res["idem"])


@need_node
def test_t4_filter_search(dump, tmp_path):
    recs = dump["dump"]["records"]
    ph0, own0 = next(r["phase"] for r in recs if r["phase"]), next(r["owner"] for r in recs)
    states = [
        {}, {"filters": {"project": ["atlas"]}}, {"filters": {"phase": [ph0]}},
        {"filters": {"state": ["ready"]}}, {"filters": {"owner": [own0]}},
        {"filters": {"priority": [2]}}, {"filters": {"kind": ["lane"]}}, {"filters": {"op": True}},
        {"filters": {"project": ["atlas"], "state": ["ready"]}},
        {"filters": {"project": ["atlas", "dune"]}},
        {"search": "ingest"}, {"search": "ingest parse"}, {"search": "state:ready"},
        {"search": "zzznomatch"},
    ]
    big_recs = dump["dump"]["recordsBig"]
    res = run_node(tmp_path, RUNNER, "t4", {"states": states})
    for got, st in zip(res["results"], states):
        assert got == contract_filter(recs, st), st
    res_b = run_node(tmp_path, RUNNER, "t4", {"states": states, "bigPath": dump["big"]})
    for got, st in zip(res_b["results"], states):
        assert got == contract_filter(big_recs, st), st
    assert res["results"][13] == [] and res["results"][8] == contract_filter(recs, states[8])


@need_node
def test_t5_sort(dump, tmp_path):
    recs = dump["dump"]["records"]
    big_recs = dump["dump"]["recordsBig"]
    proj_idx = {p["name"]: i for i, p in enumerate(dump["dump"]["projects"])}
    specs = [
        [{"col": "priority", "dir": "desc"}],
        [{"col": "state", "dir": "asc"}, {"col": "priority", "dir": "desc"}, {"col": "title", "dir": "asc"}],
        [{"col": "owner", "dir": "asc"}, {"col": "est", "dir": "desc"}],
        [{"col": "unblocks", "dir": "desc"}, {"col": "title", "dir": "asc"}],
        [{"col": "project", "dir": "desc"}, {"col": "phase", "dir": "asc"}, {"col": "priority", "dir": "desc"}],
        [{"col": "phase", "dir": "asc"}],
        [{"col": "op", "dir": "desc"}, {"col": "est", "dir": "asc"}, {"col": "owner", "dir": "asc"},
         {"col": "title", "dir": "asc"}],
        [{"col": "bogus", "dir": "asc"}],
    ]
    orig_order = [r["key"] for r in recs]
    res = run_node(tmp_path, RUNNER, "t5", {"specs": specs})
    for got, spec in zip(res["results"], specs):
        want = contract_sort(recs, spec, proj_idx)
        assert got["keys"] == want, spec
        assert got["again"] == want, spec                      # sorting sorted is a no-op
        assert got["fresh"] and got["inputAfter"] == orig_order, spec
    res_b = run_node(tmp_path, RUNNER, "t5", {"specs": specs, "bigPath": dump["big"]})
    big_proj_idx = {p["name"]: i for i, p in enumerate(dump["dump"]["projectsBig"])}
    for got, spec in zip(res_b["results"], specs):
        assert got["keys"] == contract_sort(big_recs, spec, big_proj_idx), spec
    # Stability with desc: many-tie single key (unblocks desc on BIG) — the ctx
    # order among ties is preserved (oracle tie-break), not reversed.
    stable = [{"col": "unblocks", "dir": "desc"}]
    got_stable = run_node(tmp_path, RUNNER, "t5", {"specs": [stable], "bigPath": dump["big"]})
    assert got_stable["results"][0]["keys"] == contract_sort(big_recs, stable, big_proj_idx)
