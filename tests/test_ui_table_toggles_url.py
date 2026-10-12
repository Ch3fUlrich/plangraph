"""GU-TB1 table model tests, part 3: header-click toggles, URL codec, static
guards (T9-T11). Node-run, skipped without node; oracles are plain Python
loops over the runner's record dump.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ui_table_util import (RUNNER_BOOT, WEB, need_node, run_node, vm_dump)  # noqa: E402
from test_ui_table_model import DEFAULT_COLS, contract_filter, defaults  # noqa: E402

RUNNER = RUNNER_BOOT + r"""
if (mode === "t9") {
  emit({ out: IN.cases.map(function (c) {
    var frozen = JSON.parse(JSON.stringify(c.st)); deepFreeze(frozen);
    var r = null;
    try { r = PlanUI[c.fn].apply(PlanUI, [frozen].concat(c.args)); }
    catch (e) { return { threw: String(e) }; }
    if (c.ctxAll) {
      var vm9 = vmOf(P.fix), ctx9 = PlanUI.tableContext(vm9);
      r = PlanUI[c.fn](ctx9, frozen, c.args[0]);
    }
    return { r: r, threw: false };
  }) });
} else if (mode === "t10") {
  var out10 = { goldens: IN.goldens.map(function (g) { return PlanUI.tableUrlEncode(g.st); }),
                enc: [], dec: [], norm: [] };
  IN.states.forEach(function (s) {
    var e = PlanUI.tableUrlEncode(s);
    out10.enc.push(e);
    out10.dec.push(PlanUI.tableUrlDecode(e));
    out10.norm.push(PlanUI.tableNormalize(s));
  });
  try { out10.malformed = PlanUI.tableUrlDecode(IN.malformed); out10.malformedOk = true; }
  catch (e) { out10.malformedOk = false; }
  out10.unknown = PlanUI.tableUrlDecode("x=1&t_sort=title:asc");
  out10.prefQ = PlanUI.tableUrlDecode("?t_group=none").group;
  out10.prefH = PlanUI.tableUrlDecode("#t_group=none").group;
  out10.merge = IN.merges.map(function (m) { return PlanUI.tableUrlMerge(m.search, m.st); });
  emit(out10);
} else if (mode === "names") {
  var names = ["TABLE_STATES", "TABLE_COLUMNS", "TABLE_METRICS", "tableContext", "tableDefaults",
    "tableNormalize", "tableFilter", "tableSort", "tableApply", "tableWindow", "tableStickyGroups",
    "tableFacets", "tableToggleSort", "tableToggleCollapse", "tableCollapseAll", "tableSetColumn",
    "tableSetGroup", "tableUrlEncode", "tableUrlDecode", "tableUrlMerge"];
  var types = {};
  names.forEach(function (n) { types[n] = typeof PlanUI[n]; });
  emit({ types: types });
}
"""


@pytest.fixture(scope="module")
def dump(tmp_path_factory):
    d, big = vm_dump(tmp_path_factory.mktemp("tb1toggles"))
    return {"dump": d, "big": str(big)}


@need_node
def test_t9_toggles(dump, tmp_path):
    d = defaults()

    def st(**kw):
        base = json.loads(json.dumps(d))
        base.update(kw)
        return base

    cases = [
        {"fn": "tableToggleSort", "st": d, "args": ["title", False],
         "want": {**d, "sort": [{"col": "title", "dir": "asc"}]}},
        {"fn": "tableToggleSort", "st": d, "args": ["priority", False],
         "want": {**d, "sort": [{"col": "priority", "dir": "asc"}]}},
        {"fn": "tableToggleSort", "st": st(sort=[{"col": "title", "dir": "asc"}]), "args": ["title", False],
         "want": st(sort=[{"col": "title", "dir": "desc"}])},
        {"fn": "tableToggleSort", "st": st(sort=[{"col": "title", "dir": "desc"}]), "args": ["title", False],
         "want": st(sort=[{"col": "title", "dir": "asc"}])},
        {"fn": "tableToggleSort", "st": d, "args": ["est", False],
         "want": {**d, "sort": [{"col": "est", "dir": "desc"}]}},
        {"fn": "tableToggleSort", "st": d, "args": ["title", True],
         "want": {**d, "sort": [{"col": "priority", "dir": "desc"}, {"col": "title", "dir": "asc"}]}},
        {"fn": "tableToggleSort", "st": st(sort=[{"col": "priority", "dir": "desc"}, {"col": "title", "dir": "asc"}]),
         "args": ["title", True],
         "want": st(sort=[{"col": "priority", "dir": "desc"}, {"col": "title", "dir": "desc"}])},
        {"fn": "tableToggleSort", "st": st(sort=[{"col": "title", "dir": "asc"}, {"col": "est", "dir": "desc"},
                                                 {"col": "owner", "dir": "asc"}, {"col": "state", "dir": "asc"}]),
         "args": ["priority", True],
         "want": st(sort=[{"col": "title", "dir": "asc"}, {"col": "est", "dir": "desc"},
                          {"col": "owner", "dir": "asc"}, {"col": "priority", "dir": "desc"}])},
        {"fn": "tableToggleSort", "st": st(sort=[{"col": "title", "dir": "asc"}]), "args": ["title", True],
         "want": st(sort=[{"col": "title", "dir": "desc"}])},
        {"fn": "tableToggleSort", "st": d, "args": ["bogus", False], "want": d},
        {"fn": "tableToggleCollapse", "st": d, "args": ["ph:atlas|P1 Core build"],
         "want": {**d, "collapsed": ["ph:atlas|P1 Core build"]}},
        {"fn": "tableToggleCollapse", "st": st(collapsed=["ph:atlas|P1 Core build"]),
         "args": ["ph:atlas|P1 Core build"], "want": d},
        {"fn": "tableSetColumn", "st": d, "args": ["est", False],
         "want": {**d, "cols": [c for c in DEFAULT_COLS if c != "est"]}},
        {"fn": "tableSetColumn", "st": d, "args": ["kind", True],
         "want": {**d, "cols": ["state", "project", "title", "phase", "owner", "priority",
                                "est", "kind", "op", "unblocks"]}},
        {"fn": "tableSetColumn", "st": d, "args": ["title", False], "want": d},
        {"fn": "tableSetColumn", "st": st(cols=["title"]), "args": ["title", False],
         "want": st(cols=["title"])},
        {"fn": "tableSetColumn", "st": st(cols=["title", "op"]), "args": ["op", False],
         "want": st(cols=["title"])},
        {"fn": "tableSetGroup", "st": d, "args": ["weird"], "want": d},
        {"fn": "tableSetGroup", "st": d, "args": ["none"], "want": {**d, "group": "none"}},
    ]
    res = run_node(tmp_path, RUNNER, "t9", {"cases": cases})
    for got, case in zip(res["out"], cases):
        assert not got["threw"], (case["fn"], case["args"], got["threw"])
        assert got["r"] == case["want"], (case["fn"], case["args"])
    # collapseAll(on): every p:/ph: id of the fixture exactly once; (off) -> []
    all_ids = {"ph:" + (r["project"] or "") + "|" + (r["phase"] or "")
               for r in dump["dump"]["records"]}
    all_ids |= {"p:" + p["name"] for p in dump["dump"]["projects"]}
    res_on = run_node(tmp_path, RUNNER, "t9",
                      {"cases": [{"fn": "tableCollapseAll", "st": d, "args": [True], "ctxAll": True}]})
    assert sorted(res_on["out"][0]["r"]["collapsed"]) == sorted(all_ids)
    res_off = run_node(tmp_path, RUNNER, "t9",
                       {"cases": [{"fn": "tableCollapseAll", "st": d, "args": [False], "ctxAll": True}]})
    assert res_off["out"][0]["r"]["collapsed"] == []


@need_node
def test_t10_url(dump, tmp_path):
    d = defaults()
    goldens = [
        ({"sort": [{"col": "title", "dir": "asc"}, {"col": "est", "dir": "desc"}],
          "filters": {**d["filters"], "project": ["atlas"], "phase": ["P2 Integration"],
                      "state": ["ready", "blocked"], "owner": ["B. L2"], "priority": [2],
                      "kind": ["lane"]},
          "search": "ingest parse", "cols": ["state", "est"]},
         "t_sort=title:asc,est:desc&t_cols=state,title,est&t_project=atlas&t_phase=P2%20Integration"
         "&t_state=blocked,ready&t_owner=B.%20L2&t_priority=2&t_kind=lane&t_q=ingest%20parse"),
        ({**d, "group": "none"}, "t_group=none"),
        ({"filters": {**d["filters"], "priority": [3, 1], "op": True},
          "collapsed": ["ph:atlas|P1 Core build", "p:dune"]},
         "t_collapsed=p:dune,ph:atlas|P1%20Core%20build&t_priority=1,3&t_op=1"),
    ]
    nasty = [",", "&", "=", "%", "#", "+", " ", "ünï code", "👋 emoji",
             "<script>alert(1)</script>", "a,b&c=d%e#f+g h", "ph:x|y"]
    R = __import__("random").Random(11)
    states = [d]
    for i in range(300):
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
    res = run_node(tmp_path, RUNNER, "t10", {
        "goldens": [{"st": g[0]} for g in goldens], "states": states,
        "malformed": "t_q=%E0%A4%A&t_sort=zzz:up",
        "merges": [
            {"search": "?x=1&t_q=old&y=2&t_sort=a:b", "st": {**d, "search": "new"}},
            {"search": "?x=1&y=2", "st": d},
            {"search": "", "st": d},
        ]})
    for got, (_, want) in zip(res["goldens"], goldens):
        assert got == want, got
    for enc, dec, norm in zip(res["enc"], res["dec"], res["norm"]):
        assert dec == norm
        assert "<" not in enc and " " not in enc
    assert res["malformedOk"]
    assert res["malformed"]["sort"] == defaults()["sort"]
    assert res["malformed"]["search"] == "%E0%A4%A"
    assert res["unknown"]["sort"] == [{"col": "title", "dir": "asc"}]
    assert res["prefQ"] == "none" and res["prefH"] == "none"
    m0, m1, m2 = res["merge"]
    assert m0.startswith("?x=1&y=2&")
    assert m0.split("?")[1].split("&")[:2] == ["x=1", "y=2"]
    assert m1 == "?x=1&y=2"
    assert m2 == ""


@need_node
def test_t11_static(dump, tmp_path):
    for name in ("page_table_model.js", "page_table_url.js"):
        text = (WEB / name).read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) <= 400, name
        assert not re.search(r"\b(document|location|history|innerHTML|Date|Math\.random)\b", text), name
        win = [i for i, l in enumerate(lines) if re.search(r"\bwindow\b", l)]
        assert len(win) == 1 and win[0] == len(lines) - 1, name
    from plangraph import render as render_mod
    js = render_mod.JS_FILES
    assert js.count("page_table_model.js") == 1
    assert js.index("page_table_model.js") > js.index("page_graph_view.js")
    assert js.index("page_boot.js") > js.index("page_table_model.js")
    assert js[js.index("page_table_model.js") + 1] == "page_table_url.js"
    res = run_node(tmp_path, RUNNER, "names", {})
    want = {"TABLE_STATES": "object", "TABLE_COLUMNS": "object", "TABLE_METRICS": "object"}
    for name, t in want.items():
        assert res["types"][name] == t, name
    for name, t in res["types"].items():
        if name not in want:
            assert t == "function", name
