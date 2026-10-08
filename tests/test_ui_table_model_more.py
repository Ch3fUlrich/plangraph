"""GU-TB1 table model tests, part 2: tableApply, windowing, sticky groups,
facets (T6-T8). Node-run, skipped without node; oracles are plain Python loops
over the runner's record dump. Toggles/URL/static live in
test_ui_table_toggles_url.py (T9-T11).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ui_table_util import (RUNNER_BOOT, need_node, run_node, vm_dump)  # noqa: E402
from test_ui_table_model import (STATE_IDX, TABLE_METRICS,  # noqa: E402
                                 contract_filter, contract_sort, defaults)

RUNNER = RUNNER_BOOT + r"""
function resOut(res) {
  return { total: res.total, matched: res.matched, rows: res.rows.map(function (r) { return r.key; }),
           entries: res.entries, tops: res.tops, height: res.height, headers: res.headers };
}
if (mode === "t6") {
  var ctx = PlanUI.tableContext(vmOf(IN.bigPath || P.fix));
  ctx.rows.forEach(function (r) { deepFreeze(r); });
  emit({ results: IN.cases.map(function (c) {
    var st = deepFreeze(c.state), opts = c.opts || {};
    var res = PlanUI.tableApply(ctx, st, opts);
    var o = resOut(res);
    o.res2 = resOut(PlanUI.tableApply(ctx, st, opts));
    return o;
  }) });
} else if (mode === "t7") {
  var ctx7 = PlanUI.tableContext(vmOf(IN.bigPath));
  var res7 = PlanUI.tableApply(ctx7, PlanUI.tableDefaults(), {});
  var sweep = [];
  for (var st7 = 0; st7 <= res7.height + 900; st7 += 17) {
    var w = PlanUI.tableWindow(res7, st7, 900);
    sweep.push([st7, w.start, w.end, w.padTop, w.padBottom, w.count]);
  }
  var sticky = [0, 1, 2, 5, 50, 300, 700, 1000, res7.entries.length - 1].map(function (i) {
    var sg = PlanUI.tableStickyGroups(res7, i);
    return { i: i, project: sg.project && sg.project.id, phase: sg.phase && sg.phase.id };
  });
  var none = PlanUI.tableApply(ctx7, { group: "none" }, {});
  var edges = {
    nan: PlanUI.tableWindow(res7, NaN, 900), neg: PlanUI.tableWindow(res7, -5, 900),
    big: PlanUI.tableWindow(res7, res7.height + 100000, 900), vh0: PlanUI.tableWindow(res7, 100, 0),
    vhneg: PlanUI.tableWindow(res7, 100, -5), empty: PlanUI.tableWindow(PlanUI.tableApply(PlanUI.tableContext({}), {}, {}), 10, 900),
    ovs0: PlanUI.tableWindow(res7, 100, 900, 0), ovsDef: PlanUI.tableWindow(res7, 100, 900),
    ovs20: PlanUI.tableWindow(res7, 100, 900, 20), hm900: PlanUI.tableWindow(res7, res7.height - 900, 900)
  };
  emit({ tops: res7.tops, height: res7.height, n: res7.entries.length, sweep: sweep,
         sticky: sticky, groups: res7.entries.map(function (e, i) {
           return e.type === "group" ? { i: i, level: e.level, id: e.id, project: e.project } : null;
         }).filter(Boolean),
         noneSticky: [0, 100].map(function (i) {
           var sg = PlanUI.tableStickyGroups(none, i);
           return { project: sg.project && sg.project.id, phase: sg.phase && sg.phase.id };
         }), edges: edges, types: res7.entries.map(function (e) { return e.type; }) });
} else if (mode === "t8") {
  emit({ results: IN.cases.map(function (c) {
    var ctx8 = PlanUI.tableContext(vmOf(c.bigPath || P.fix));
    ctx8.rows.forEach(function (r) { deepFreeze(r); });
    var st = deepFreeze(JSON.parse(JSON.stringify(c.st)));
    return { facets: PlanUI.tableFacets(ctx8, st), matched: PlanUI.tableFilter(ctx8, st).length };
  }) });
} else if (mode === "t8x") { emit({}); }
"""



FACETS = ["project", "phase", "state", "owner", "priority", "kind"]


def rec_tops(entries, m):
    tops, acc = [0], 0
    for e in entries:
        acc += m["groupH"] if e["type"] == "group" else m["rowH"] if e["type"] == "row" else m["detailH"]
        tops.append(acc)
    return tops


def state_counts(rows):
    d = sum(1 for r in rows if r["display"] == "done")
    r = sum(1 for r in rows if r["display"] == "ready")
    b = sum(1 for r in rows if r["display"] == "blocked")
    return {"done": d, "ready": r, "blocked": b}


def apply_oracle(records, projects, st, expanded=(), m=None):
    """Independent oracle for tableApply (C8), plain loops."""
    m = m or TABLE_METRICS
    group = st.get("group", "project>phase")
    if group != "none":
        # group-ignored keys are simply dropped; with nothing left the order
        # degrades to the ctx-index tie-break (C7) — NOT priority desc
        spec = [s for s in st.get("sort", defaults()["sort"]) if s["col"] not in ("project", "phase")]
    else:
        spec = st.get("sort") or [{"col": "priority", "dir": "desc"}]
    proj_idx = {p["name"]: i for i, p in enumerate(projects)}
    keys = contract_filter(records, st)
    by_key = {r["key"]: r for r in records}
    frows = [by_key[k] for k in keys]
    srows = [next(r for r in records if r["key"] == k)
             for k in contract_sort(frows, spec, proj_idx)]
    entries, headers, rows_out = [], [], []
    color = {p["name"]: p["color"] for p in projects}

    def emit_row(r):
        rows_out.append(r)
        entries.append({"type": "row", "row": r, "color": color[r["project"] or ""]})
        if r["key"] in expanded:
            entries.append({"type": "detail", "row": r, "color": color[r["project"] or ""]})

    if group == "none":
        for r in srows:
            emit_row(r)
    else:
        for p in projects:
            plist = [r for r in srows if (r["project"] or "") == p["name"]]
            if not plist:
                continue
            all_n = sum(1 for r in records if (r["project"] or "") == p["name"])
            pid = "p:" + p["name"]
            headers.append(len(entries))
            entries.append({"type": "group", "level": "project", "id": pid, "project": p["name"],
                            "phase": None, "label": p["label"], "color": p["color"],
                            "count": len(plist), "all": all_n, **state_counts(plist),
                            "collapsed": pid in st.get("collapsed", [])})
            if pid in st.get("collapsed", []):
                continue
            phases = []
            for r in plist:
                if r["phase"] not in phases:
                    phases.append(r["phase"])
            phases.sort(key=lambda ph: (next(x["phaseIdx"] for x in plist if x["phase"] == ph), ph))
            for ph in phases:
                ph_rows = [r for r in plist if r["phase"] == ph]
                ph_all = sum(1 for r in records if (r["project"] or "") == p["name"]
                             and (r["phase"] or "") == ph)
                phid = "ph:" + p["name"] + "|" + ph
                headers.append(len(entries))
                entries.append({"type": "group", "level": "phase", "id": phid, "project": p["name"],
                                "phase": ph, "label": ph, "color": p["color"],
                                "count": len(ph_rows), "all": ph_all, **state_counts(ph_rows),
                                "collapsed": phid in st.get("collapsed", [])})
                if phid not in st.get("collapsed", []):
                    for r in ph_rows:
                        emit_row(r)
    tops = rec_tops(entries, m)
    return {"total": len(records), "matched": len(srows),
            "rows": [r["key"] for r in (srows if group != "none" else rows_out)],
            "entries": entries, "tops": tops, "height": tops[-1], "headers": headers}


def prune(got):
    """Group entries compared field by field; row/detail entries by key + color."""
    out = {k: v for k, v in got.items() if k not in ("entries", "rows2", "res2")}
    out["rows"] = got["rows"]
    out["entries"] = [{k: v for k, v in e.items() if k != "row"} if e["type"] == "group"
                      else {"type": e["type"], "key": e["row"]["key"], "color": e["color"]}
                      for e in got["entries"]]
    return out


def prune_want(want):
    want = dict(want)
    want["entries"] = [{k: v for k, v in e.items() if k != "row"} if e["type"] == "group"
                       else {"type": e["type"], "key": e["row"]["key"], "color": e["color"]}
                       for e in want["entries"]]
    return want


@pytest.fixture(scope="module")
def dump(tmp_path_factory):
    d, big = vm_dump(tmp_path_factory.mktemp("tb1more"))
    return {"dump": d, "big": str(big)}


@need_node
def test_t6_apply(dump, tmp_path):
    recs = dump["dump"]["records"]
    projs = dump["dump"]["projects"]
    k0 = next(r["key"] for r in recs if r["display"] not in ("done", "cancelled"))
    cases = [
        {"state": defaults()},
        {"state": {**defaults(), "filters": {**defaults()["filters"], "project": ["atlas"]}}},
        {"state": {**defaults(), "collapsed": ["ph:atlas|P1 Core build"]}},
        {"state": {**defaults(), "collapsed": ["p:borealis"]}},
        {"state": {**defaults(), "group": "none",
                   "sort": [{"col": "project", "dir": "desc"}, {"col": "phase", "dir": "asc"},
                            {"col": "priority", "dir": "desc"}]}},
        {"state": {**defaults(), "sort": [{"col": "project", "dir": "asc"}]}},
        {"state": {**defaults(), "filters": {**defaults()["filters"], "state": ["ready"]}}},
        {"state": defaults(), "opts": {"expanded": [k0], "metrics": {"rowH": 10, "groupH": 5, "detailH": 7}}},
        {"state": defaults(), "opts": {"metrics": {"rowH": 0, "groupH": 0, "detailH": 0}}},
        {"state": defaults(), "opts": {"metrics": {"rowH": 50}}},
    ]
    res = run_node(tmp_path, RUNNER, "t6", {"cases": cases})
    for i, (got, case) in enumerate(zip(res["results"], cases)):
        raw_m = (case.get("opts") or {}).get("metrics") or {}
        want = apply_oracle(recs, projs, case["state"],
                            expanded=(case.get("opts") or {}).get("expanded", ()),
                            m={**TABLE_METRICS, **raw_m})
        assert prune(got) == prune_want(want), f"case {i}"
        assert prune(got["res2"]) == prune_want(want)            # same state twice, deep-equal
        assert got["total"] == len(recs) and got["matched"] == len(got["rows"])
    # grouped apply on the BIG vm too (phase labels S0..S5+S10 discriminate
    # phaseIdx order from plain label order)
    big_recs, big_projs = dump["dump"]["recordsBig"], dump["dump"]["projectsBig"]
    res_b = run_node(tmp_path, RUNNER, "t6", {"cases": [{"state": defaults()}],
                                              "bigPath": dump["big"]})
    want_b = apply_oracle(big_recs, big_projs, defaults())
    assert prune(res_b["results"][0]) == prune_want(want_b)
    # collapsed phase header present, headers are group indexes
    ph_i = res["results"][2]["headers"]
    assert all(res["results"][2]["entries"][i]["type"] == "group" for i in ph_i)


@need_node
def test_t6_apply_group_omissions(dump, tmp_path):
    """Groups without matching rows are omitted (fixture: filter to one project)."""
    res = run_node(tmp_path, RUNNER, "t6", {
        "cases": [{"state": {**defaults(), "filters": {**defaults()["filters"], "project": ["atlas"]}}}]})
    entries = res["results"][0]["entries"]
    projs_in = {e["project"] for e in entries if e["type"] == "group"}
    assert projs_in == {"atlas"}


@need_node
def test_t7_window_sweep(dump, tmp_path):
    res = run_node(tmp_path, RUNNER, "t7", {"bigPath": dump["big"]})
    tops, n, height = res["tops"], res["n"], res["height"]
    assert len(tops) == n + 1 and tops[0] == 0 and tops[-1] == height
    assert all(tops[i] <= tops[i + 1] for i in range(n))
    m = TABLE_METRICS
    want_tops = []
    acc = 0
    for t in res["types"]:
        acc += m["groupH"] if t == "group" else m["rowH"] if t == "row" else m["detailH"]
        want_tops.append(acc)
    assert want_tops == tops[1:]

    import bisect

    def raw_start(st):
        return next((i for i in range(n) if tops[i + 1] > st), n)

    def raw_end(st, vh):
        return next((i for i in range(len(tops)) if tops[i] >= st + vh), n)

    for raw_st, start, end, pad_top, pad_bot, count in res["sweep"]:
        st = min(raw_st, height)                              # C9: scrollTop > height clamps
        rs = raw_start(st)
        re_ = raw_end(st, 900)
        w_start, w_end = max(0, rs - 6), min(n, re_ + 6)
        assert (start, end, count) == (w_start, w_end, w_end - w_start), raw_st
        assert rs < end or rs == n, raw_st
        assert tops[end] >= min(st + 900, height) and tops[start] <= st, raw_st
        assert pad_top == tops[start] and pad_bot == height - tops[end], raw_st
        assert pad_top + (tops[end] - tops[start]) + pad_bot == height, raw_st
        assert count <= 42, raw_st
    assert res["edges"]["hm900"]["end"] == n
    # sticky groups: recompute the nearest preceding headers per C9 and compare
    def sticky_oracle(index):
        prior = [g for g in res["groups"] if g["i"] <= min(index, n - 1)]
        if not prior:
            return {"project": None, "phase": None}
        last = prior[-1]
        if last["level"] == "project":
            return {"project": last["id"], "phase": None}
        proj = next((g for g in reversed(prior) if g["level"] == "project"), None)
        phase = last["id"]
        if proj is None or proj["project"] != last["project"]:
            phase = None
        return {"project": proj["id"] if proj else None, "phase": phase}

    for s in res["sticky"]:
        got = {"project": s["project"], "phase": s["phase"]}
        assert got == sticky_oracle(s["i"]), s["i"]
    assert res["noneSticky"] == [{"project": None, "phase": None}] * 2
    e = res["edges"]
    assert e["nan"]["start"] == 0 and e["neg"]["start"] == 0
    rs100 = raw_start(100)
    re100 = raw_end(100, 900)
    exact = min(n, rs100 + 6) - max(0, rs100 - 6)          # overscan-only window
    assert e["vh0"]["count"] == exact and e["vhneg"]["count"] == exact
    assert e["ovs0"]["count"] == min(n, re100) - rs100
    assert e["ovsDef"]["count"] == min(n, re100 + 6) - max(0, rs100 - 6)
    assert e["ovs20"]["count"] == min(n, re100 + 20) - max(0, rs100 - 20)
    assert e["big"]["end"] == n and e["big"]["start"] == n - 6 and e["big"]["count"] == 6
    assert e["empty"] == {"start": 0, "end": 0, "padTop": 0, "padBottom": 0, "count": 0}
    st_mid = res["sweep"][len(res["sweep"]) // 2]
    assert st_mid[5] <= 42


@need_node
def test_t8_facets(dump, tmp_path):
    recs = dump["dump"]["records"]
    big_recs = dump["dump"]["recordsBig"]
    projs = dump["dump"]["projects"]
    big_projs = dump["dump"]["projectsBig"]
    label = {p["name"]: p["label"] for p in projs}
    big_label = {p["name"]: p["label"] for p in big_projs}
    proj_idx = {p["name"]: i for i, p in enumerate(projs)}
    big_proj_idx = {p["name"]: i for i, p in enumerate(big_projs)}
    st = {"filters": {**defaults()["filters"], "project": ["atlas"], "state": ["ready"]},
          "search": "evidence"}

    def facet_oracle(fn, records, projects, state):
        lab = {p["name"]: p["label"] for p in projects}
        pidx = {p["name"]: i for i, p in enumerate(projects)}
        sub = {k: v for k, v in state["filters"].items() if k != fn}
        keys = contract_filter(records, {"filters": sub, "search": state.get("search", "")})
        by_key = {r["key"]: r for r in records}
        counts = {}
        for k in keys:
            r = by_key[k]
            v = int(r["priority"] or 0) if fn == "priority" else \
                r["display"] if fn == "state" else (r[fn] or "")
            counts[v] = counts.get(v, 0) + 1
        for v in state["filters"].get(fn) or []:
            vv = int(v) if fn == "priority" else v
            counts.setdefault(vv, 0)
        items = []
        for v, c in counts.items():
            items.append({"value": v, "label": lab.get(v, str(v)) if fn == "project" else str(v),
                          "count": c})
        if fn == "project":
            items.sort(key=lambda it: (pidx.get(it["value"], 9999), it["label"]))
        elif fn == "phase":
            items.sort(key=lambda it: natural_key(it["value"]))
        elif fn == "state":
            items.sort(key=lambda it: STATE_IDX.get(it["value"], 99))
        elif fn == "owner":
            items.sort(key=lambda it: (-it["count"], it["value"]))
        elif fn == "priority":
            items.sort(key=lambda it: -int(it["value"]))
        else:
            items.sort(key=lambda it: it["value"])
        return items

    from test_ui_table_model import natural_key  # noqa: E402  (import late: local module)
    # non-degenerate states: defaults on the fixture AND on BIG (S10 discriminates
    # the natural phase order), plus the original filtered state (matched > 0)
    cases = [
        {"st": defaults(), "recs": recs, "projs": projs, "pidx": proj_idx, "lbl": label,
         "bigPath": None},
        {"st": defaults(), "recs": big_recs, "projs": big_projs, "pidx": big_proj_idx,
         "lbl": big_label, "bigPath": dump["big"]},
        {"st": st, "recs": recs, "projs": projs, "pidx": proj_idx, "lbl": label,
         "bigPath": None},
        # C10: a selected value with 0 matches is still listed at count 0
        {"st": {**defaults(), "filters": {**defaults()["filters"], "phase": ["NOPE"]}},
         "recs": recs, "projs": projs, "pidx": proj_idx, "lbl": label,
         "bigPath": None, "zero": ("phase", "NOPE")},
    ]
    res = run_node(tmp_path, RUNNER, "t8", {"cases": [{"st": c["st"], "bigPath": c["bigPath"]}
                                                      for c in cases]})
    for ci, (got, case) in enumerate(zip(res["results"], cases)):
        if "zero" not in case:
            assert got["matched"] > 0, f"case {ci} matches nothing — facet counts are vacuous"
        for fn in FACETS:
            want = facet_oracle(fn, case["recs"], case["projs"], case["st"])
            assert [{**it} for it in got["facets"][fn]] == want, (ci, fn)
        if "zero" in case:
            fn, val = case["zero"]
            zero_items = [it for it in got["facets"][fn] if it["value"] == val]
            assert len(zero_items) == 1 and zero_items[0]["count"] == 0, (ci, fn, val)
            assert any(it["count"] > 0 for it in got["facets"][fn]), (ci, fn)
        need = lambda r: r["operator"] and r["display"] not in ("done", "cancelled")  # noqa: E731
        sub = {k: v for k, v in case["st"]["filters"].items() if k != "op"}
        keys = contract_filter(case["recs"], {"filters": sub, "search": case["st"].get("search", "")})
        by_key = {r["key"]: r for r in case["recs"]}
        rows = [by_key[k] for k in keys]
        assert got["facets"]["op"] == {"on": sum(1 for r in rows if need(r)),
                                       "off": sum(1 for r in rows if not need(r))}
    # project facet totals without other filters == group sizes (case 0 = fixture defaults)
    sizes = {}
    for r in recs:
        sizes[r["project"] or ""] = sizes.get(r["project"] or "", 0) + 1
    for it in res["results"][0]["facets"]["project"]:
        assert it["count"] == sizes[it["value"]]
