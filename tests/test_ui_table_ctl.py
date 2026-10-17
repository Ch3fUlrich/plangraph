"""Tests for PlanUI.TableCtl: facet filters, column hiding, URL persistence."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
"use strict";
var path = require("path");
globalThis.PlanUI = {};
var web = process.argv[2];
require(path.join(web, "page_table_model.js"));
require(path.join(web, "page_table_url.js"));
require(path.join(web, "page_table_ctl.js"));
var ctl = globalThis.PlanUI.TableCtl;
var rows = [
  { key: "t1", state: "ready", display: "ready", owner: "alice", priority: 2, kind: "decision", operator: "a" },
  { key: "t2", state: "blocked", display: "blocked", owner: "bob", priority: 5, kind: "question" },
  { key: "t3", state: "done", display: "done", owner: "alice", priority: 9, kind: "decision", operator: "b" },
  { key: "t4", state: "cancelled", display: "cancelled", owner: "carol", priority: 0, kind: "", operator: "c" },
  { key: "t5", state: "open", display: "in_review", owner: "", priority: 3, kind: "question", operator: "d" }
];
function keys(rs) { return rs.map(function (r) { return r.key; }).sort().join(","); }
function set(q) { ctl.reset(); ctl.load(q); }
var o = {};
set("?t_state=done"); o.state = keys(rows.filter(ctl.pass));
set("?t_owner=alice"); o.owner = keys(rows.filter(ctl.pass));
set("?t_priority=2,3"); o.priority = keys(rows.filter(ctl.pass));
set("?t_kind=question"); o.kind = keys(rows.filter(ctl.pass));
set("?t_op=1"); o.op = keys(rows.filter(ctl.pass));
set(""); o.all = keys(rows.filter(ctl.pass));
o.opNeeded = keys(rows.filter(ctl.operatorNeeded));
ctl.reset();
ctl.load("?t_state=ready&t_owner=alice&t_op=1");
ctl.state.hide = { prio: true, kind: true };
var q = ctl.save();
ctl.reset();
ctl.load(q);
o.rtState = JSON.stringify(ctl.state.filters.state);
o.rtOwner = JSON.stringify(ctl.state.filters.owner);
o.rtOp = ctl.state.filters.op;
o.rtHide = Object.keys(ctl.state.hide).sort().join(",");
o.rtRows = keys(rows.filter(ctl.pass));
o.rtSaveSame = ctl.save() === q;
ctl.reset();
o.defRows = keys(rows.filter(ctl.pass));
o.defSave = ctl.save();
ctl.reset();
ctl.state.filters.state = ["a&t_hide=x", "</script>"];
ctl.state.hide = { "a&t_hide=x": true };
var h = ctl.save();
var ps = h.replace(/^\?/, "").split("&").filter(Boolean);
o.hKeys = ps.map(function (p) { var i = p.indexOf("="); return i < 0 ? p : p.slice(0, i); }).sort().join("|");
o.hRawScript = h.indexOf("</script>") >= 0;
o.hHide = ps.filter(function (p) { return p.indexOf("t_hide=") === 0; }).map(function (p) { return p.slice(7); }).join("");
console.log(JSON.stringify(o));
"""

@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("table_ctl")
    js = d / "table_ctl.js"
    js.write_text(SCRIPT, encoding="utf-8")
    proc = subprocess.run([NODE, str(js), str(WEB)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout)

def test_pass_facets(out):
    assert out["state"] == "t3"
    assert out["owner"] == "t1,t3"
    assert out["priority"] == "t1,t5"
    assert out["kind"] == "t2,t5"
    assert out["op"] == "t1,t5"          # done + cancelled rows excluded
    assert out["opNeeded"] == "t1,t5"

def test_save_load_roundtrip(out):
    assert out["rtState"] == '["ready"]'
    assert out["rtOwner"] == '["alice"]'
    assert out["rtOp"] is True
    assert out["rtHide"] == "kind,prio"
    assert out["rtRows"] == "t1"
    assert out["rtSaveSame"] is True

def test_defaults_pass_all_and_empty_url(out):
    assert out["all"] == "t1,t2,t3,t4,t5"
    assert out["defRows"] == "t1,t2,t3,t4,t5"
    assert out["defSave"] == ""

def test_hostile_values_cannot_inject_params(out):
    assert out["hKeys"] == "t_hide|t_state"
    assert out["hRawScript"] is False
    assert out["hHide"] == "a%26t_hide%3Dx"
