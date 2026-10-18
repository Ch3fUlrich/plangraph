import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"
node = shutil.which("node")

JS = r"""
globalThis.PlanUI = {
  naturalCmp: function (a, b) {
    var x = String(a).match(/\d+|\D+/g) || [], y = String(b).match(/\d+|\D+/g) || [], i = 0;
    for (; i < x.length && i < y.length; i++) {
      var nx = /^\d+$/.test(x[i]), ny = /^\d+$/.test(y[i]);
      if (nx && ny) { if (+x[i] !== +y[i]) return +x[i] - +y[i]; }
      else if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    }
    return x.length - y.length;
  },
  QAPage: { effectiveStatus: function (q) { return q.answer ? "answered" : q.status; } }
};
var mod = require(process.argv[2]);
var NP = mod && mod.rows ? mod : globalThis.PlanUI.NeedsPage;
var now = Date.UTC(2024, 0, 15), day = 86400000;
var iso = function (ms) { return new Date(ms).toISOString(); };
var vm = {
  needsYou: [
    {key: "T1", title: "Task 10", project: "Alpha", phase: "dev", display: "doing", priority: 3,
     unblocks: [], wait: "task", updatedAt: iso(now - day), createdAt: iso(now - 3 * day)},
    {key: "T2", title: "Task 2", project: "Beta", phase: "dev", display: "todo", priority: 1,
     unblocks: [], wait: "answer", createdAt: iso(now - 5 * day)},
    {id: "Q1", text: "Alpha question", project: "Alpha"}
  ],
  questions: [
    {id: "Q1", project: "Alpha", text: "Alpha question", asked_by: "a", status: "open", item: "T1", answer: ""},
    {id: "Q2", project: "Beta", text: "Beta question", asked_by: "b", status: "open", item: "", answer: ""},
    {id: "Q3", project: "Alpha", text: "Answered question", asked_by: "c", status: "answered", item: "", answer: "yes"}
  ],
  decisions: [
    {id: "D1", title: "Decision one", text: "d1", date: iso(now - 10 * day).slice(0, 10), project: "Alpha", items: ["T1"]},
    {id: "D2", title: "Decision two", text: "d2", date: iso(now - 40 * day).slice(0, 10), project: "Gamma", items: ["T2"]}
  ],
  items: new Map([["T1", {}], ["T2", {}]]),
  projects: [{name: "Alpha", color: "#111"}, {name: "Beta", color: "#222"}, {name: "Gamma", color: "#333"}]
};
var rows = NP.rows(vm, now);
var ids = function (rs) { return rs.map(function (r) { return r.id; }); };
var by = {};
rows.forEach(function (r) { by[r.id] = r; });
var mk = function (o) { return {type: o.t || [], project: o.p || [], priority: o.i || [], state: o.s || [], age: o.a || ""}; };
var f0 = mk({}), out = {};
out.count = rows.length;
out.task = rows.filter(function (r) { return r.type === "task"; }).length;
out.question = rows.filter(function (r) { return r.type === "question"; }).length;
out.decision = rows.filter(function (r) { return r.type === "decision"; }).length;
out.ages = [by.D1.ageDays, by.T1.ageDays, by.T2.ageDays, by.D2.ageDays, by.Q1.ageDays, by.Q2.ageDays];
out.states = [by.Q1.state, by.Q3.state, by.D1.state, by.T1.state];
out.fType = ids(NP.filter(rows, mk({t: ["decision"]}), now));
out.fProj = ids(NP.filter(rows, mk({p: ["Alpha"]}), now));
out.fPrio = ids(NP.filter(rows, mk({i: [3]}), now));
out.fState = ids(NP.filter(rows, mk({s: ["open"]}), now));
out.fAge7 = ids(NP.filter(rows, mk({a: "7"}), now));
out.fAge30 = ids(NP.filter(rows, mk({a: "30+"}), now));
var pri = NP.sort(rows, {col: "priority", dir: "desc"});
out.priTop = ids(pri).slice(0, 2);
out.priRest = ids(pri).slice(2);
out.priRestNull = pri.slice(2).every(function (r) { return r.priority === null || r.priority === undefined; });
var tit = ids(NP.sort(rows, {col: "title", dir: "asc"}));
out.titNatural = tit.indexOf("T2") < tit.indexOf("T1");
out.encDefault = NP.encode(f0, {col: "priority", dir: "desc"});
var f1 = mk({t: ["task", "question"], p: ["Alpha"], i: [3], s: ["open"], a: "7"});
out.rt = NP.decode(NP.encode(f1, {col: "title", dir: "asc"}));
out.rtOK = JSON.stringify([out.rt.f, out.rt.sort]) === JSON.stringify([f1, {col: "title", dir: "asc"}]);
out.h1 = NP.decode(NP.encode(mk({p: ["a&n_type=x"]}), {col: "age", dir: "desc"}));
out.h2 = NP.decode(NP.encode(mk({p: ["</script>"]}), {col: "id", dir: "asc"}));
out.g1 = NP.decode("n_priority=abc&n_age=99&n_sort=bogus:zz&n_row=zz");
console.log(JSON.stringify(out));
"""

@pytest.mark.skipif(not node, reason="node not available")
def test_needs_page_pure(tmp_path):
    js = tmp_path / "needs.js"
    js.write_text(JS, encoding="utf-8")
    res = subprocess.run([node, str(js), str(WEB / "page_needs.js")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # (1) unified rows
    assert out["count"] == 7
    assert (out["task"], out["question"], out["decision"]) == (2, 3, 2)
    assert out["ages"] == [10, 1, 5, 40, None, None]
    assert out["states"] == ["open", "answered", "decided", "doing"]
    # (2) filters
    assert out["fType"] == ["D1", "D2"]
    assert sorted(out["fProj"]) == ["D1", "Q1", "Q3", "T1"]
    assert out["fPrio"] == ["T1"]
    assert sorted(out["fState"]) == ["Q1", "Q2"]
    assert sorted(out["fAge7"]) == ["T1", "T2"]
    assert out["fAge30"] == ["D2"]
    # (3) sort
    assert out["priTop"] == ["T1", "T2"]
    assert out["priRest"] == ["Q1", "Q2", "Q3", "D1", "D2"]
    assert out["priRestNull"] is True
    assert out["titNatural"] is True
    # (4) encode/decode
    assert out["encDefault"] == ""
    assert out["rtOK"] is True
    assert out["h1"]["f"] == {"type": [], "project": ["a&n_type=x"], "priority": [], "state": [], "age": ""}
    assert out["h1"]["sort"] == {"col": "age", "dir": "desc"}
    assert out["h2"]["f"]["type"] == [] and out["h2"]["f"]["project"] == ["</script>"]
    assert out["g1"] == {"f": {"type": [], "project": [], "priority": [], "state": [], "age": ""},
                         "sort": {"col": "priority", "dir": "desc"}, "row": "zz"}

def test_needs_page_static():
    for name in ("page_needs.js", "page_needs_view.js"):
        src = (WEB / name).read_text(encoding="utf-8")
        assert "innerHTML" not in src, name
        assert "eval(" not in src, name



def test_boot_reads_pg_hash_before_first_write():
    # Mutation that fails it: pg= read only in afterInit, after writeHash dropped it (deep link lands on Graph).
    src = (WEB / "page_boot.js").read_text(encoding="utf-8")
    assert "var HASH_PG" in src and "HASH_PG ||" in src
