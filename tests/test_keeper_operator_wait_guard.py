"""Operator-wait linking contracts; World follows PG_KEEPER_BACKEND in CI."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from keeper_util import World, defn, to_claimed, to_done
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("WORK"), defn("OP", operator=True), defn("OTHER"),
        defn("GOAL", ["WORK", "OP", "OTHER"], kind="goal")])
    yield world
    world.close()


def test_v1_holder_adds_operator_audited(w):
    epoch = to_claimed(w, "demo:WORK", "workerA")
    before = w.store._item("demo:WORK")
    result = w.store.edit_definition(w.p("workerA"), "demo:WORK", {"depends_on": ["OP"]})
    after = w.store._item("demo:WORK")
    assert result["epoch"] == epoch + 1
    assert after["round"] == (before["round"] or 0) + 1
    assert after["claimed_by"] == "workerA"
    event = w.conn.execute("SELECT payload FROM events WHERE key=? AND type='item_defined' ORDER BY seq DESC", ("demo:WORK",)).fetchone()
    payload = json.loads(event["payload"])
    assert payload["who"] == "workerA"
    assert payload["before"] == {"depends_on": []}
    assert payload["after"] == {"depends_on": ["demo:OP"]}
    assert w.store.verify_integrity() == []


def test_v2_holder_nonoperator_forbidden(w):
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("workerA"), "demo:WORK", {"depends_on": ["OTHER"]})
    assert exc.value.code == "forbidden"


def test_v3_holder_removal_forbidden(w):
    to_claimed(w, "demo:WORK", "workerA")
    w.store.edit_definition(w.p("router"), "demo:WORK", {"depends_on": ["OTHER"]})
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("workerA"), "demo:WORK", {"depends_on": ["OP"]})
    assert exc.value.code == "forbidden"


def test_v4_nonholder_forbidden(w):
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("workerB"), "demo:WORK", {"depends_on": ["OP"]})
    assert exc.value.code == "forbidden"


def test_v5_router_unchanged(w):
    epoch = to_claimed(w, "demo:WORK", "workerA")
    result = w.store.edit_definition(w.p("router"), "demo:WORK", {"depends_on": ["OTHER"]})
    assert result["epoch"] == epoch + 1
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:OTHER"]
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("field,text", [("note", "IDLE until operator"), ("description", "waiting on OS-12"), ("done_criteria", "waiting_on: operator approval")])
@pytest.mark.parametrize("state", ["claimed", "in_review"])
def test_v6_unlinked_open_and_done_operator(w, field, text, state):
    w.store.edit_definition(w.p("router"), "demo:WORK", {field: text})
    to_claimed(w, "demo:WORK", "workerA")
    if state == "in_review":
        epoch = w.store._item("demo:WORK")["epoch"]
        w.store.add_evidence(w.p("workerA"), "demo:WORK", epoch, "decision", {"id": "D-1"})
        w.store.submit(w.p("workerA"), "demo:WORK", epoch)
    def flags():
        return [r for r in w.store.needs_you(w.p("reader")) if r.get("kind") == "operator_wait_unlinked"]
    assert [r["key"] for r in flags()] == ["demo:WORK"]
    w.store.edit_definition(w.p("router"), "demo:WORK", {"depends_on": ["OP"]})
    assert flags() == []
    to_done(w, "demo:OP")
    assert [r["key"] for r in flags()] == ["demo:WORK"]


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
@pytest.mark.parametrize("case", ["base", "missing_ref", "missing_key", "prefer_wait", "prefer_wait_reverse", "headers", "facets"])
def test_flag_row_rendered(case):
    web = Path(__file__).resolve().parents[1] / "src/plangraph/web"
    script = r"""
const nodes=[];
function el(){const n={children:[],style:{setProperty(){}},appendChild(x){this.children.push(x)},removeChild(x){this.children.splice(this.children.indexOf(x),1)},get firstChild(){return this.children[0]},set textContent(x){this.children=[];this.text=String(x)},get textContent(){return (this.text||'')+this.children.map(c=>c.textContent).join('')},setAttribute(){},addEventListener(){}};nodes.push(n);return n;}
const host=el();global.document={createElement:el,getElementById(id){return id==='sb-panel-needs'?host:null}};
global.location={search:'',pathname:''};
require(process.argv[1]+'/page_needs.js');
require(process.argv[1]+'/page_needs_view.js');
const it={key:'demo:WORK',title:'Waiting',project:'demo',state:'claimed',doneCriteria:'IDLE until operator',deps:[]};
const flag={...it,kind:'operator_wait_unlinked',wait:'task'};
for(const items of [new Map([[it.key,it]]),[it]]) {
 PlanUI.NeedsPage.render({items,needsYou:[flag,flag]});
 if(!host.textContent.includes('Operator wait unlinked'))throw Error('flag missing');
 if(!host.textContent.includes('demo:WORK'))throw Error('key missing');
 if(!host.textContent.includes('(1 of 1)'))throw Error('duplicate flag');
 if(!host.textContent.includes('claimed'))throw Error('state missing');
 PlanUI.NeedsPage.render({items,needsYou:[]});
 if(host.textContent.includes('Operator wait unlinked'))throw Error('client derived flag');
}
const row={type:'task',id:'a',title:'A',project:'demo',priority:1,state:''};
const mode=process.argv[2];
let rs=[];
if(mode==='missing_ref')rs=[row,{...row,type:'question',id:'q'}];
if(mode==='missing_key')rs=[{...row,ref:{}},{...row,id:'b',ref:{}},{...row,id:'',ref:{}},{...row,id:'',ref:{}}];
if(mode.startsWith('prefer_wait'))rs=[{...row,key:'a',ref:{kind:'operator_wait_unlinked'}},{...row,key:'a',title:'Human question',ref:{kind:'question',wait:'answer'}}];
if(mode==='prefer_wait_reverse')rs.reverse();
if(mode==='headers')rs=[{...row,ref:{}}];
if(mode==='facets')rs=[{...row,key:'a',project:'a',priority:'odd',ref:{}},{...row,key:'b',id:'b',project:'Z',priority:2,ref:{}}];
if(rs.length){
 if(mode==='missing_ref')rs.forEach(Object.freeze);
 PlanUI.NeedsPage.rows=()=>rs;
 if(mode==='facets'){
  String.prototype.localeCompare=function(){throw Error('locale dependent')};
  const original=Array.prototype.sort;
  Array.prototype.sort=function(cmp){return original.call(this,cmp?function(a,b){const n=cmp(a,b);if(Number.isNaN(n))throw Error('NaN comparator');return n}:cmp)};
 }
 PlanUI.NeedsPage.render({});
 if(mode==='missing_key'&&!host.textContent.includes('(4 of 4)'))throw Error('keyless row lost');
 if(mode.startsWith('prefer_wait')&&(!host.textContent.includes('Human question')||host.textContent.includes('Operator wait unlinked')))throw Error('human wait lost');
 if(mode==='headers'){
  const table=host.children.find(n=>n.className==='tt ny-t');
  const hs=table.children[0].children[0].children;
  ['type','key','title','project','priority','state','age'].forEach((k,i)=>{if(hs[i].className!=='tc-'+k)throw Error('header class '+k)});
 }
}
"""
    result = subprocess.run([shutil.which("node"), "-e", script, str(web), case], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("text", ["no operator intervention needed", "operator approval", "OS-12 is documented"])
def test_wait_requires_marker(w, text):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert not any(r.get("kind") == "operator_wait_unlinked" for r in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("value", [["IDLE until operator"], {"waiting_on": "operator"}, 12])
def test_nonstring_wait_fields_do_not_crash(w, value, monkeypatch):
    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item["note"] = value
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    w.store.needs_you(w.p("reader"))


def test_needs_scope(w):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": "IDLE until operator"})
    to_claimed(w, "demo:WORK", "workerA")
    assert w.store.needs_you(w.p("l1other")) == []
    assert w.store.needs_you(w.p("l1other"), project="demo") == []
    assert any(r["key"] == "demo:WORK" for r in w.store.needs_you(w.p("workerA")))


@pytest.mark.parametrize("signal", ["operator", "OS-12", "task", ""])
def test_wait_signals_are_additive(w, monkeypatch, signal):
    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(waiting_on=signal, note="IDLE until operator")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    rows = w.store.needs_you(w.p("reader"))
    assert any(r.get("kind") == "operator_wait_unlinked" for r in rows)


@pytest.mark.parametrize("text", ["Status: IDLE until operator", "waiting_on: operator",
                                      "Waiting on OS-123", "Notes. IDLE until operator",
                                      "Notes; waiting on operator", "Notes\nIDLE until operator"])
def test_labeled_wait_marker(w, text):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert any(r.get("kind") == "operator_wait_unlinked" for r in w.store.needs_you(w.p("reader")))


def test_genuine_wait_preferred(w, monkeypatch):
    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(kind="question", note="IDLE until operator")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    rows = [r for r in w.store.needs_you(w.p("reader")) if r["key"] == "demo:WORK"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "question"


@pytest.mark.parametrize("text", ["Work is waiting on OS-12", "Notes about IDLE until operator",
                                      "IDLE notes: see ticket operator-123",
                                      "IDLE until operator-sync", "IDLE until team-operator-sync",
                                      "IDLE until operator_x"])
def test_wait_rejects_prose_and_identifiers(w, text):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert not any(r.get("kind") == "operator_wait_unlinked"
                   for r in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("signal,expected", [("operator", True), ("OS-123", True),
    ("operator approval", True), ("operator-123", False), ("operator-sync", False),
    ("team-operator-sync", False), ("operator_x", False)])
def test_waiting_on_standalone_operator(w, monkeypatch, signal, expected):
    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(waiting_on=signal)
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    assert any(r.get("kind") == "operator_wait_unlinked"
               for r in w.store.needs_you(w.p("reader"))) == expected


@pytest.mark.parametrize("kind", ["question", "operator_task"])
@pytest.mark.parametrize("reverse", [False, True])
def test_genuine_wait_preferred_in_both_row_orders(w, monkeypatch, kind, reverse):
    from plangraph.keeper import store_needs

    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(note="IDLE until operator")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    human = {"key": item["key"], "kind": kind, "wait": "answer" if kind == "question" else "task"}
    flag = {"key": item["key"], "kind": "operator_wait_unlinked", "wait": "task"}
    rows = [flag, human] if reverse else [human, flag]
    monkeypatch.setattr(store_needs.views, "needs_you", lambda *args, **kwargs: rows)
    assert w.store.needs_you(w.p("reader")) == [human]


def test_missing_project_and_empty_scope(w, monkeypatch):
    from dataclasses import replace
    item = w.store._item("demo:WORK")
    item.pop("project")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    assert w.store.needs_you(w.p("workerA")) == []
    item.update(project="demo", operator=True)
    assert w.store._needs_you(None, replace(w.p("workerA"), projects=[])) == []
