"""GU-TB1 acceptance A31 (T12, measured): performance budgets on the BIG
snapshot (1200 and 2400 rows). 2 warm-ups then the MEDIAN of 7 runs, every run
with a DIFFERENT argument so no result can be cached. The numbers are reported
in one json line; the asserts only enforce the task budgets.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ui_table_util import (BIG_PHASES, BIG_PROJECTS, RUNNER_BOOT, big_snapshot,  # noqa: E402
                           need_node, run_node)
from test_ui_table_model import DEFAULT_COLS, defaults  # noqa: E402

RUNNER = RUNNER_BOOT + r"""
function median(arr) { var s = arr.slice().sort(function (a, b) { return a - b; }); return s[s.length >> 1]; }
function bench(fn, argsList, warm) {
  for (var w = 0; w < warm; w++) fn(argsList[w % argsList.length]);
  var times = argsList.map(function (a) {
    var t0 = process.hrtime.bigint(), r = fn(a), t1 = process.hrtime.bigint();
    return { ms: Number(t1 - t0) / 1e6, r: r };
  });
  var mss = times.map(function (t) { return t.ms; });
  return { median: median(mss), min: Math.min.apply(null, mss), max: Math.max.apply(null, mss) };
}
if (mode === "a31") {
  var vms = IN.snaps.map(function (p) { return PlanUI.buildViewModel(JSON.parse(fs.readFileSync(p, "utf8"))); });
  var big = vms[0], big2 = vms[7];
  var out = { node: process.version, rows: big.items.size, rows2: big2.items.size, ms: {} };
  out.ms.tableContext = bench(function (vm) { return PlanUI.tableContext(vm); }, vms.slice(0, 7), 2);
  var ctx = PlanUI.tableContext(big), ctx2 = PlanUI.tableContext(big2);
  out.ms.tableFilter = bench(function (st) {
    return PlanUI.tableFilter(ctx, st).map(function (r) { return r.key; });
  }, IN.filterStates, 2);
  out.ms.search = bench(function (q) {
    var st = { filters: PlanUI.tableDefaults().filters, search: q };
    return PlanUI.tableFilter(ctx, st).map(function (r) { return r.key; });
  }, IN.searches, 2);
  out.ms.sort2 = bench(function (spec) {
    return PlanUI.tableSort(ctx.rows, { sort: spec, group: "none" }, ctx);
  }, IN.sorts2, 2);
  out.ms.sort4 = bench(function (spec) {
    return PlanUI.tableSort(ctx.rows, { sort: spec, group: "none" }, ctx);
  }, IN.sorts4, 2);
  function applyCase(c, cx) {
    var st = deepFreeze(JSON.parse(JSON.stringify(c.st)));
    var expanded = PlanUI.tableFilter(cx, st).slice(0, 40).map(function (r) { return r.key; });
    return PlanUI.tableApply(cx, st, { expanded: expanded });
  }
  out.applyMatched = IN.applyCases.map(function (c) { return PlanUI.tableFilter(ctx, c.st).length; });
  out.ms.apply = bench(function (c) { return applyCase(c, ctx); }, IN.applyCases, 2);
  out.ms.apply2400 = bench(function (c) { return applyCase(c, ctx2); }, IN.applyCases, 2);
  out.ms.facets = bench(function (st) { return PlanUI.tableFacets(ctx, st); }, IN.facetStates, 2);
  var res7 = PlanUI.tableApply(ctx, PlanUI.tableDefaults(), {});
  var t0 = process.hrtime.bigint(), wSum = 0, N = 1000, lastSt = 0;
  for (var i = 0; i < N; i++) { lastSt = Math.floor((res7.height * i) / N); wSum += PlanUI.tableWindow(res7, lastSt, 900).count; }
  var t1 = process.hrtime.bigint();
  out.windowMeanMs = Number(t1 - t0) / 1e6 / N;
  var win = [0, Math.floor(res7.height / 2), res7.height - 900].map(function (st) {
    return PlanUI.tableWindow(res7, st, 900);
  });
  out.rendered = { counts: win.map(function (w) { return w.count; }), matched: res7.matched };
  var mism = 0;
  IN.urlStates.forEach(function (s) {
    var e = PlanUI.tableUrlEncode(s);
    if (JSON.stringify(PlanUI.tableUrlDecode(e)) !== JSON.stringify(PlanUI.tableNormalize(s))) mism++;
  });
  out.urlStates = IN.urlStates.length;
  out.urlMismatches = mism;
  var tU = bench(function (st) {
    var e = PlanUI.tableUrlEncode(st);
    return PlanUI.tableUrlDecode(e);
  }, IN.urlStates.slice(0, 7), 2);
  out.ms.url = tU;
  emit(out);
}
"""


def nasty_states(n=300, seed=11):
    d = defaults()
    nasty = [",", "&", "=", "%", "#", "+", " ", "ünï code", "👋 emoji",
             "<script>alert(1)</script>", "a,b&c=d%e#f+g h", "ph:x|y"]
    R = random.Random(seed)
    states = []
    for i in range(n):
        st = json.loads(json.dumps(d))
        st["search"] = R.choice(nasty) + " s" + str(i)
        st["sort"] = R.choice([[{"col": "title", "dir": "asc"}],
                               [{"col": "est", "dir": "desc"}, {"col": "priority", "dir": "asc"}], []])
        st["filters"]["project"] = R.choice([[], ["atlas"], [""], ["at,l%as"]])
        st["filters"]["phase"] = R.choice([[], ["P2 Integration"], [""], ["P9%H"]])
        st["filters"]["owner"] = R.choice([[], ["Owner-03"], [""], ["O,3"]])
        st["filters"]["kind"] = R.choice([[], ["lane"], ["gate"], ["la,n%e"]])
        st["filters"]["priority"] = R.choice([[], [1, 3], [0]])
        st["filters"]["op"] = R.choice([False, True])
        st["group"] = R.choice(["project>phase", "none"])
        st["collapsed"] = R.choice([[], ["p:atlas"], ["ph:atlas|P1 Core build", "p:dune"],
                                    ["p:a%2Cl"]])
        st["cols"] = R.choice([DEFAULT_COLS, ["title", "est"], ["kind", "title", "state"]])
        states.append(st)
    return states


@need_node
def test_a31_measured(tmp_path):
    snaps = []
    for i, per in enumerate([150] * 7 + [300]):
        snap = big_snapshot(per)
        cut = i * 173                       # rotate: 7 DISTINCT vm args for the context bench
        snap["items"] = snap["items"][cut:] + snap["items"][:cut]
        p = tmp_path / f"big{i}.json"
        p.write_text(json.dumps(snap), encoding="utf-8")
        snaps.append(str(p))
    d = defaults()
    words = ["ingest", "parse", "shard", "route", "cache", "verify", "sign"]
    filter_states, search_states = [], []
    sorts2, sorts4, apply_cases, facet_states = [], [], [], []
    pair_pool = [("title", "asc"), ("owner", "desc"), ("kind", "asc"), ("state", "desc"),
                 ("unblocks", "asc"), ("op", "desc"), ("project", "asc")]
    last_pool = ["title", "kind", "project", "phase", "est", "unblocks", "op"]
    apply_variants = ["state:ready", "state:blocked", "ingest", "parse",
                      "state:in_review", "state:claimed", "audit"]
    for i in range(7):
        # 4 facets with REAL BIG values (non-empty result, real work)
        filter_states.append({"filters": {**d["filters"], "project": [BIG_PROJECTS[i % 8]],
                                          "phase": [BIG_PHASES[i % 6]], "state": ["ready"],
                                          "owner": [f"Owner-{i:02d}"]}})
        search_states.append(f"{words[i]} {words[(i + 3) % 7]} state:ready")
        sorts2.append([{"col": pair_pool[i][0], "dir": pair_pool[i][1]},
                       {"col": "est", "dir": "desc"}])
        sorts4.append([{"col": "state", "dir": "asc"}, {"col": "priority", "dir": "desc"},
                       {"col": "owner", "dir": "asc"}, {"col": last_pool[i], "dir": "asc"}])
        # seven DISTINCT apply arguments (facet+search variants, each non-empty)
        apply_cases.append({"st": {**d, "search": apply_variants[i],
                                   "filters": {**d["filters"], "kind": ["lane"]}}})
        facet_states.append({**d, "search": words[i]})
    res = run_node(tmp_path, RUNNER, "a31", {
        "snaps": snaps, "filterStates": filter_states, "searches": search_states,
        "sorts2": sorts2, "sorts4": sorts4, "applyCases": apply_cases,
        "facetStates": facet_states, "urlStates": nasty_states()}, timeout=300)

    ms = res["ms"]
    assert ms["tableContext"]["median"] < 50
    assert ms["tableFilter"]["median"] < 50
    assert ms["search"]["median"] < 50
    assert ms["sort2"]["median"] < 50 and ms["sort4"]["median"] < 50
    assert ms["apply"]["median"] < 50
    assert ms["apply2400"]["median"] < 100
    assert ms["facets"]["median"] < 50
    assert res["windowMeanMs"] < 0.05
    assert ms["url"]["median"] < 5
    assert res["rows"] == 1200 and res["rows2"] == 2400
    assert min(res["applyMatched"]) >= 40          # apply benches a real result, not an empty one
    for c in res["rendered"]["counts"]:
        assert c <= 42 and c < res["rendered"]["matched"]
    assert res["urlMismatches"] == 0 and res["urlStates"] == 300

    report = {"node": res["node"], "rows": res["rows"], "rows2": res["rows2"],
              "ms": {k: round(v["median"], 3) for k, v in ms.items()},
              "min": {k: round(v["min"], 3) for k, v in ms.items()},
              "max": {k: round(v["max"], 3) for k, v in ms.items()},
              "windowMeanMs": round(res["windowMeanMs"], 5),
              "rendered": res["rendered"], "urlMismatches": res["urlMismatches"]}
    print("A31 " + json.dumps(report))
