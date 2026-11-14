"""The real Rust owner gate is exercised through keeper definition writes."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path, monkeypatch):
    binary = Path(__file__).resolve().parents[1] / "rust/target/debug/pg-operator-owner-gate"
    monkeypatch.setenv("PG_OPERATOR_OWNER_GATE_BIN", str(binary))
    monkeypatch.setenv("PG_OPERATOR_OWNER_GATE_MODE", "refuse")
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def question(risk="taste", **kw):
    return defn("Q", kind="question", owner="operator", risk_tag=risk,
                choices={"options": [{"id": "yes", "label": "Yes"}],
                         "default": "yes", "deadline": "2027-01-01"}, **kw)


@pytest.mark.parametrize("risk", ["money", "access", "secret", "public", "irreversible", "live", "taste"])
def test_operator_owner_gate_stores_each_risk(w, risk):
    w.store.create_items(w.p("router"), [question(risk), defn("G2", ["Q"], kind="goal")])
    assert w.store.show(w.p("reader"), "demo:Q")["risk_tag"] == risk
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("change", [{"risk_tag": None}, {"risk_tag": "other"},
                                   {"choices": None}, {"kind": "lane", "choices": None}, {"id": "RULE-X"}])
def test_operator_owner_gate_refuses_create(w, change):
    raw = question()
    raw.update(change)
    with pytest.raises(Refused, match="operator_owner_gate"):
        w.store.create_items(w.p("router"), [raw])


def test_operator_owner_gate_edit_and_hard_lock(w):
    w.store.create_items(w.p("router"), [question("money"), defn("G2", ["Q"], kind="goal")])
    for fields in ({"owner": "routing", "operator": False}, {"risk_tag": "taste"}):
        with pytest.raises(Refused, match="trusted operator answer"):
            w.store.edit_definition(w.p("router"), "demo:Q", fields)
    assert w.store._item("demo:Q")["owner"] == "operator"


def test_operator_owner_gate_warn_is_default(w, monkeypatch):
    monkeypatch.delenv("PG_OPERATOR_OWNER_GATE_MODE")
    result = w.store.create_items(w.p("router"), [defn("Q", operator=True),
                  defn("G2", ["Q"], kind="goal")])
    assert any("operator_owner_gate" in warning for warning in result["warnings"])


def test_operator_owner_gate_until(w):
    w.store.create_items(w.p("router"), [question(until="2099-01-01"),
                         defn("G2", ["Q"], kind="goal")])
    assert not any(row["key"] == "demo:Q" for row in w.store.needs_you(w.p("reader")))
    w.store.edit_definition(w.p("router"), "demo:Q", {"until": "2000-01-01"})
    assert any(row["key"] == "demo:Q" for row in w.store.needs_you(w.p("reader")))


@pytest.mark.parametrize("track,title", [
    ("merge_with_go", "Request a merge with GO"),
    ("service_restart", "Restart a service"),
    ("secret_creation_until_phase1", "Create secret until phase 1"),
    ("continuity_go_within_budget", "Continuity GO within budget"),
])
def test_operator_owner_gate_standing_grants(w, track, title):
    raw = question()
    raw.update(track=track, title=title)
    with pytest.raises(Refused, match="D-2579"):
        w.store.create_items(w.p("router"), [raw])
    raw["track"] = "OTHER"
    with pytest.raises(Refused, match="D-2579"):
        w.store.create_items(w.p("router"), [raw])


def test_operator_owner_gate_trusts_authenticated_answer_only(w):
    w.store.create_items(w.p("router"), [question("money"), defn("G2", ["Q"], kind="goal")])
    qid = w.store.ask_question(w.p("router"), "demo:Q", "Approve ownership change?")["id"]
    w.store.answer_question(w.p("router"), qid, "yes")
    with pytest.raises(Refused, match="trusted operator answer"):
        w.store.edit_definition(w.p("router"), "demo:Q", {"risk_tag": "taste"})
    token = auth.add_principal(w.conn, "operator", "router", [])
    operator = auth.authenticate(w.conn, token)
    qid = w.store.ask_question(w.p("router"), "demo:Q", "Approve ownership change?")["id"]
    w.store.answer_question(operator, qid, "yes")
    w.store.edit_definition(w.p("router"), "demo:Q", {"risk_tag": "taste"})
    assert w.store._item("demo:Q")["risk_tag"] == "taste"
    assert verify_integrity(w.conn) == []


def test_operator_owner_gate_operator_flag_and_router_edit(w):
    raw = question()
    raw.update(owner="routing", operator=True)
    w.store.create_items(w.p("router"), [raw, defn("G2", ["Q"], kind="goal")])
    with pytest.raises(Refused) as denied:
        w.store.edit_definition(w.p("l1main"), "demo:Q", {"risk_tag": "live"})
    assert denied.value.code == "forbidden"
    w.store.edit_definition(w.p("router"), "demo:Q", {"risk_tag": "live"})
    with pytest.raises(Refused, match="trusted operator answer"):
        w.store.edit_definition(w.p("router"), "demo:Q", {"operator": False})


@pytest.mark.parametrize("missing", ["default", "deadline"])
def test_operator_owner_gate_missing_choice_fields(w, missing):
    raw = question()
    raw["choices"].pop(missing)
    with pytest.raises(Refused, match="choices.default and choices.deadline"):
        w.store.create_items(w.p("router"), [raw])


def test_operator_owner_gate_snapshot_parking(w):
    w.store.create_items(w.p("router"), [question(until="2099-01-01"),
                         defn("G2", ["Q"], kind="goal")])
    row = next(item for item in w.store.snapshot(w.p("reader"))["items"] if item["id"] == "Q")
    assert row["risk_tag"] == "taste" and row["needs_you_hidden"] is True
    assert row["until"] == "2099-01-01"


def test_operator_owner_gate_kind_conversion_cannot_bypass(w):
    with pytest.raises(Refused, match="operator_owner_gate"):
        w.store.set_kind(w.p("router"), "demo:A", "question")
    assert w.store._item("demo:A")["kind"] == "lane"


def test_operator_owner_gate_helper_unavailable_refuses(w, monkeypatch):
    monkeypatch.setenv("PG_OPERATOR_OWNER_GATE_BIN", "/missing/operator-owner-gate")
    with pytest.raises(Refused, match="helper unavailable"):
        w.store.create_items(w.p("router"), [question()])


def test_operator_owner_gate_item_payload_cannot_forge_answer(w):
    raw = question("money", operator_principal="router", events=[{
        "type": "question_answered", "actor": "operator", "role": "router",
        "seq": 999, "payload": {"source": "answer", "risk_tag": "money", "epoch": 0}}])
    w.store.create_items(w.p("router"), [raw, defn("G2", ["Q"], kind="goal")])
    with pytest.raises(Refused, match="trusted operator answer"):
        w.store.edit_definition(w.p("router"), "demo:Q", {"risk_tag": "taste"})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_operator_owner_gate_page_hides_parked_questions_after_refresh():
    web = Path(__file__).resolve().parents[1] / "src/plangraph/web"
    script = """
const web = process.argv[1];
global.PlanGraph = require(web + '/page.js');
const ui = require(web + '/page_model.js');
const vm = ui.buildViewModel({items:[
 {key:'demo:Q',id:'Q',project:'demo',kind:'question',owner:'operator',operator:true,
  state:'open',title:'Question',needs_you_hidden:true,depends_on:[]},
 {key:'demo:T',id:'T',project:'demo',kind:'operator_task',owner:'operator',operator:false,
  state:'open',title:'Task',needs_you_hidden:false,depends_on:[]}]});
const before = vm.needsYou.map(x => x.key || x.item);
ui.refreshQA(vm, {});
process.stdout.write(JSON.stringify([before,vm.needsYou.map(x => x.key || x.item)]));
"""
    output = subprocess.check_output(["node", "-e", script, str(web)], text=True)
    assert json.loads(output) == [["demo:T"], ["demo:T"]]
