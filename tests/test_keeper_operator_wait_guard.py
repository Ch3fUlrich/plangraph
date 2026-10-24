"""Operator-wait linking contracts; World follows PG_KEEPER_BACKEND in CI."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from keeper_util import World, defn, to_claimed, to_done
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import read_events


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
    payload = [e["payload"] for e in read_events(w.conn)
               if e["key"] == "demo:WORK" and e["type"] == "item_defined"][-1]
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
@pytest.mark.parametrize("case", ["base", "missing_ref", "missing_key", "prefer_wait", "prefer_wait_reverse", "headers", "facets", "two_real", "aria", "server"])
def test_flag_row_rendered(case, w):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": "IDLE until operator"})
    to_claimed(w, "demo:WORK", "workerA")
    server_rows = w.store.needs_you(w.p("reader"))
    web = Path(__file__).resolve().parents[1] / "src/plangraph/web"
    script = r"""
const nodes=[];
function el(){const n={children:[],attrs:{},style:{setProperty(){}},appendChild(x){this.children.push(x)},removeChild(x){this.children.splice(this.children.indexOf(x),1)},get firstChild(){return this.children[0]},set textContent(x){this.children=[];this.text=String(x)},get textContent(){return (this.text||'')+this.children.map(c=>c.textContent).join('')},setAttribute(k,v){this.attrs[k]=v},addEventListener(){}};nodes.push(n);return n;}
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
if(mode==='server'){
 PlanUI.NeedsPage.render({needsYou:JSON.parse(process.argv[3])});
 if(!host.textContent.includes('Operator wait unlinked')||!host.textContent.includes('demo:WORK')||!host.textContent.includes('claimed'))throw Error('server flag missing');
}
let rs=[];
if(mode==='two_real')rs=[{...row,key:'a',title:'First wait',ref:{wait:'task'}},{...row,key:'a',title:'Second wait',ref:{wait:'answer'}}];
if(mode==='aria')rs=[{...row,id:'Q-1',key:'demo:WORK',ref:{}}];
if(mode==='missing_ref')rs=[row,{...row,type:'question',id:'q'}];
if(mode==='missing_key')rs=[{...row,ref:{}},{...row,id:'b',ref:{}},{...row,id:'',ref:{}},{...row,id:'',ref:{}}];
if(mode.startsWith('prefer_wait'))rs=[{...row,key:'a',ref:{kind:'operator_wait_unlinked'}},{...row,key:'a',title:'Human question',ref:{kind:'question',wait:'answer'}}];
if(mode==='prefer_wait_reverse')rs.reverse();
if(mode==='headers')rs=[{...row,ref:{}}];
if(mode==='facets')rs=[{...row,key:'a',project:'a',priority:'odd',ref:{}},{...row,key:'b',id:'b',project:'Z',priority:2,ref:{}}];
if(rs.length){
  nodes.length=0;
 if(mode==='missing_ref')rs.forEach(Object.freeze);
 PlanUI.NeedsPage.rows=()=>rs;
 if(mode==='facets'){
  String.prototype.localeCompare=function(){throw Error('locale dependent')};
  const original=Array.prototype.sort;
  Array.prototype.sort=function(cmp){return original.call(this,cmp?function(a,b){const n=cmp(a,b);if(Number.isNaN(n))throw Error('NaN comparator');return n}:cmp)};
 }
  PlanUI.NeedsPage.render({});
  if(mode==='two_real'&&(!host.textContent.includes('First wait')||!host.textContent.includes('Second wait')||!host.textContent.includes('(2 of 2)')))throw Error('genuine wait lost');
  if(mode==='aria'&&!nodes.some(n=>n.attrs['aria-label']==='Jump to demo:WORK'))throw Error('jump label lacks key');
 if(mode==='missing_key'&&!host.textContent.includes('(4 of 4)'))throw Error('keyless row lost');
 if(mode.startsWith('prefer_wait')&&(!host.textContent.includes('Human question')||host.textContent.includes('Operator wait unlinked')))throw Error('human wait lost');
 if(mode==='headers'){
  const table=host.children.find(n=>n.className==='tt ny-t');
  const hs=table.children[0].children[0].children;
  ['type','key','title','project','priority','state','age'].forEach((k,i)=>{if(hs[i].className!=='tc-'+k)throw Error('header class '+k)});
 }
}
"""
    result = subprocess.run([shutil.which("node"), "-e", script, str(web), case, json.dumps(server_rows)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_question_row_drops_flag_end_to_end(w, monkeypatch):
    """V5 (finding 5): one precedence rule, server first and JS defensive.

    A genuine ``type: "question"`` row for the same key beats the diagnostic
    flag: the server drops the flag in ``_needs_you`` and the view repeats
    that same rule as defence.  The mapper runs for real (no ``rows()`` stub).
    """
    from plangraph.keeper import store_needs

    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": "IDLE until operator"})
    to_claimed(w, "demo:WORK", "workerA")
    server_rows = w.store.needs_you(w.p("reader"))
    flag = [r for r in server_rows if r.get("kind") == "operator_wait_unlinked"]
    assert len(flag) == 1
    question = {"key": "demo:WORK", "kind": "question", "wait": "answer"}
    with monkeypatch.context() as patch:
        patch.setattr(store_needs.views, "needs_you",
                      lambda *args, **kwargs: [question, flag[0]])
        assert w.store.needs_you(w.p("reader")) == [question]

    web = Path(__file__).resolve().parents[1] / "src/plangraph/web"
    script = r"""
const nodes=[];
function el(){const n={children:[],attrs:{},style:{setProperty(){}},appendChild(x){this.children.push(x)},removeChild(x){this.children.splice(this.children.indexOf(x),1)},get firstChild(){return this.children[0]},set textContent(x){this.children=[];this.text=String(x)},get textContent(){return (this.text||'')+this.children.map(c=>c.textContent).join('')},setAttribute(k,v){this.attrs[k]=v},addEventListener(){}};nodes.push(n);return n;}
const host=el();global.document={createElement:el,getElementById(id){return id==='sb-panel-needs'?host:null}};
global.location={search:'',pathname:''};
require(process.argv[1]+'/page_needs.js');
require(process.argv[1]+'/page_needs_view.js');
const vm={needsYou:JSON.parse(process.argv[2]),
          questions:[{id:'Q-1', project:'demo', text:'Q: ship it?', status:'open', item:'demo:WORK'}]};
const raw=PlanUI.NeedsPage.rows(vm, Date.now());
if(raw.length!==2)throw Error('real mapper derives flag + question, got '+raw.length);
PlanUI.NeedsPage.render(vm);
const txt=host.textContent;
if(!txt.includes('Q: ship it?'))throw Error('question row lost: '+txt);
if(txt.includes('Operator wait unlinked'))throw Error('flag survived a genuine question row');
if(!txt.includes('(1 of 1)'))throw Error('expected one deduped row: '+txt);
"""
    result = subprocess.run([shutil.which("node"), "-e", script, str(web), json.dumps(flag)], capture_output=True, text=True)
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
    assert any(r["key"] == "demo:WORK" for r in w.store.needs_you(w.p("l1other")))
    assert any(r["key"] == "demo:WORK" for r in w.store.needs_you(w.p("l1other"), project="demo"))
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


@pytest.mark.parametrize("text", ["Notes about IDLE until operator",
                                      "IDLE notes: see ticket operator-123",
                                      "IDLE until operator-sync", "IDLE until team-operator-sync",
                                      "IDLE until operator_x"])
def test_wait_rejects_prose_and_identifiers(w, text):
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert not any(r.get("kind") == "operator_wait_unlinked"
                   for r in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("text", [
    "IDLE until the operator",                  # article after the connective
    "waiting on the operator",                  # article, other marker
    "Work is waiting on OS-12",                 # mid-sentence verb phrase
    "waiting_on: operator not confirmed",       # colon form, negation of the awaited state
    "IDLE until operator approval, not urgent yet",  # negation after the wait clause
])
def test_wait_false_negatives_flag(w, text):
    """V3: the five reported phrases must flag."""
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert any(r.get("kind") == "operator_wait_unlinked"
               for r in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("text", [
    "Work is not waiting on operator",          # negation governing the wait
    "waiting_on: operator sign-off not required",  # negation of necessity
    "IDLE until no operator needed",            # negation before the target
])
def test_negated_waits_stay_unflagged(w, text):
    """V3: a negation that governs the wait itself still suppresses the flag."""
    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": text})
    to_claimed(w, "demo:WORK", "workerA")
    assert not any(r.get("kind") == "operator_wait_unlinked"
                   for r in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("signal,expected", [("operator", True), ("OS-123", True),
    ("operator approval", True),
    # D-2458/2: one rule, so the numeric-suffix tokens flag on this path too
    # (the IDLE/prose path already accepted them; see the V2 test below).
    ("operator-123", True), ("operator-sync", False),
    ("team-operator-sync", False), ("operator_x", False)])
def test_waiting_on_standalone_operator(w, monkeypatch, signal, expected):
    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(waiting_on=signal)
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    assert any(r.get("kind") == "operator_wait_unlinked"
               for r in w.store.needs_you(w.p("reader"))) == expected


@pytest.mark.parametrize("signal,expected", [
    ("operator-123", True), ("OS-123-B", True),
    ("operator-sync", False), ("operator_x", False)])
def test_operator_token_rule_identical_on_both_paths(w, monkeypatch, signal, expected):
    """V2 (finding 2): one rule for ``_OPERATOR`` and ``_WAIT``.

    The ``waiting_on`` field and the IDLE/prose paths must read a hyphenated
    token the same way, whichever way each phrase is spelled.
    """
    to_claimed(w, "demo:WORK", "workerA")
    base = w.store._item("demo:WORK")

    def flags(**update):
        item = dict(base)
        item.update(update)
        monkeypatch.setattr(w.store, "_all_items", lambda: [item])
        return any(r.get("kind") == "operator_wait_unlinked"
                   for r in w.store.needs_you(w.p("reader")))

    assert flags(waiting_on=signal) == expected          # waiting_on path
    assert flags(note="IDLE until " + signal) == expected  # IDLE/prose path
    assert flags(note="waiting on " + signal) == expected  # prose, other marker


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
    assert any(r["key"] == "demo:WORK" for r in
               w.store._needs_you(None, replace(w.p("workerA"), projects=[])))


@pytest.mark.parametrize("role", ["router", "reader", "system", "l1", "l2", "worker", "reviewer"])
@pytest.mark.parametrize("projects", [None, [], ["other"], ["demo"]])
def test_needs_read_role_matrix(w, role, projects):
    from dataclasses import replace
    from plangraph.keeper import auth

    w.store.edit_definition(w.p("router"), "demo:WORK", {"note": "IDLE until operator"})
    to_claimed(w, "demo:WORK", "workerA")
    if role == "l2":
        with pytest.raises(Refused, match="unknown role"):
            auth.add_principal(w.conn, "matrix", role, projects or [])
        return
    token = auth.add_principal(w.conn, "matrix", role, projects or [])
    principal = replace(auth.authenticate(w.conn, token), projects=projects)
    assert w.store.show(principal, "demo:WORK")["key"] == "demo:WORK"
    assert any(r["key"] == "demo:WORK" for r in w.store.needs_you(principal))
    assert w.store.needs_you(principal, project="other") == []


@pytest.mark.parametrize("text", [
    "- IDLE until operator", "* IDLE until operator", "> waiting on operator",
    "**IDLE until operator**", "[IDLE until operator]", "1. IDLE until operator",
    "Task 12: IDLE until operator", "state=IDLE until operator",
    "Current status for this card: IDLE until operator",
    "IDLE until operator-123", "waiting on operator-123", "IDLE until OS-123-B",
    "waiting on OS-123-B",
])
def test_formatted_and_contextual_waits(w, text):
    test_labeled_wait_marker(w, text)


@pytest.mark.parametrize("text", [
    "IDLE is not a blocker, the operator can proceed",
    "waiting on the client; no operator needed",
    "IDLE today, operator handoff was last week",
    "IDLE until no operator needed", "IDLE not waiting for operator",
    "IDLE without operator", "IDLE until " + "x" * 31 + " operator",
])
def test_nonwait_clauses(w, text):
    test_wait_rejects_prose_and_identifiers(w, text)


@pytest.mark.parametrize("signal,expected", [
    ("operator sign-off not required", False), ("no operator needed", False),
    ("not waiting for operator", False), ("without operator", False),
    ("see ticket operator-123", False),
    # D-2458/2: one rule for both paths -- the prose form below already
    # accepted this token, so the waiting_on field must too.
    ("OS-123-B", True),
    ("IDLE until operator-123", True), ("waiting on OS-123-B", True),
])
def test_waiting_on_marker_consistency(w, monkeypatch, signal, expected):
    test_waiting_on_standalone_operator(w, monkeypatch, signal, expected)


@pytest.mark.parametrize("with_human", [False, True])
def test_preference_preserves_keyless_and_other_rows(w, monkeypatch, with_human):
    from plangraph.keeper import store_needs

    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(note="IDLE until operator")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    keyless = [{"kind": "question"}, {"kind": "operator_wait_unlinked"}]
    flag = {"key": item["key"], "kind": "operator_wait_unlinked"}
    human = {"key": item["key"], "kind": "question"}
    rows = keyless + [flag] + ([human] if with_human else [])
    monkeypatch.setattr(store_needs.views, "needs_you", lambda *args, **kwargs: rows)
    result = w.store.needs_you(w.p("reader"))
    assert all(row in result for row in keyless)
    assert (human in result) == with_human
    assert (flag in result) != with_human


def test_loop_adds_only_absent_flag_keys(w, monkeypatch):
    from plangraph.keeper import store_needs

    to_claimed(w, "demo:WORK", "workerA")
    item = w.store._item("demo:WORK")
    item.update(note="IDLE until operator")
    monkeypatch.setattr(w.store, "_all_items", lambda: [item])
    human = {"key": item["key"], "kind": "question"}
    for rows in ([], [human]):
        monkeypatch.setattr(store_needs.views, "needs_you", lambda *args, **kwargs: rows)
        result = w.store.needs_you(w.p("reader"))
        assert len(result) == 1
        assert result[0]["kind"] == ("question" if rows else "operator_wait_unlinked")
