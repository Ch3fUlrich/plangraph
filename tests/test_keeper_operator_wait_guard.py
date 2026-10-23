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


@pytest.mark.parametrize("field,text", [("note", "IDLE until operator"), ("description", "waiting on OS-12"), ("done_criteria", "operator approval")])
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
def test_flag_row_rendered():
    web = Path(__file__).resolve().parents[1] / "src/plangraph/web"
    script = r"""
const nodes=[];
function el(){const n={children:[],style:{},appendChild(x){this.children.push(x)},setAttribute(){},addEventListener(){}};nodes.push(n);return n;}
const host=el();global.document={createElement:el,getElementById(){return host}};
global.location={search:'',pathname:''};
require(process.argv[1]+'/page_needs.js');
require(process.argv[1]+'/page_needs_view.js');
const it={key:'demo:WORK',title:'Waiting',project:'demo',state:'claimed',doneCriteria:'IDLE until operator',deps:[]};
PlanUI.NeedsPage.render({items:new Map([[it.key,it]]),needsYou:[]});
if(!nodes.some(n=>n.textContent==='Operator wait unlinked'))throw Error('flag missing');
if(!nodes.some(n=>n.textContent==='demo:WORK'))throw Error('key missing');
"""
    result = subprocess.run([shutil.which("node"), "-e", script, str(web)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
