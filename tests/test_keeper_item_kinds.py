"""MK-KINDS: item kinds question/decision/operator_task with operator/wait defaults.

Mutation killed by these tests: dropping ``KIND_DEFAULTS`` entries makes
``test_defaults_per_kind`` fail; letting ``_normalise`` skip ``check_kind`` makes
``test_unknown_kind_refused`` fail.
"""
import pytest
import yaml

from keeper_util import World, defn, seed
from plangraph.keeper import importer
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def _item(w, key):
    return w.store._item(key)


@pytest.mark.parametrize("kind,wait", [
    ("question", "answer"), ("decision", "answer"), ("operator_task", "task")])
def test_defaults_per_kind(w, kind, wait):
    w.store.create_items(w.p("router"), [
        defn("Q", kind=kind, operator=None), defn("G2", ["Q"], kind="goal")])
    q = _item(w, "demo:Q")
    assert q["kind"] == kind and q["wait"] == wait and q["operator"] == 1
    info = w.store.show(w.p("reader"), "demo:Q")
    assert info["wait"] == wait and info["waits_on_operator"] is True
    assert verify_integrity(w.conn) == []


def test_defaults_when_keys_absent(w):
    item = defn("Q", kind="question")
    del item["operator"]
    w.store.create_items(w.p("router"), [item, defn("G2", ["Q"], kind="goal")])
    assert _item(w, "demo:Q")["operator"] == 1 and _item(w, "demo:Q")["wait"] == "answer"


def test_explicit_values_win(w):
    w.store.create_items(w.p("router"), [
        defn("Q", kind="question", operator=False, wait="task"),
        defn("N", kind="decision", wait=None, operator=None),
        defn("G2", ["Q", "N"], kind="goal")])
    q, n = _item(w, "demo:Q"), _item(w, "demo:N")
    assert q["operator"] == 0 and q["wait"] == "task"
    assert n["wait"] is None and n["operator"] == 1
    assert verify_integrity(w.conn) == []


def test_other_kinds_get_no_defaults(w):
    assert _item(w, "demo:A")["wait"] is None and _item(w, "demo:A")["operator"] == 0
    assert _item(w, "demo:GOAL")["wait"] is None


@pytest.mark.parametrize("bad", ["task", "Question", "", None, 5, ["lane"]])
def test_unknown_kind_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", kind=bad)])
    assert exc.value.code == "validation" and "kind" in exc.value.msg
    assert _item(w, "demo:X") is None


def test_bad_explicit_wait_still_refused(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("Q", kind="question", wait="later")])
    assert "wait" in exc.value.msg


def test_question_blocks_dependents_like_a_lane(w):
    w.store.create_items(w.p("router"), [
        defn("Q", kind="question", operator=None), defn("D", ["Q"]),
        defn("G2", ["D"], kind="goal")])
    ready = w.store.ready(w.p("reader"))
    assert "demo:Q" in ready and "demo:D" not in ready
    assert "demo:D" in str(w.store.blocked(w.p("reader")))


def test_snapshot_carries_kind(w):
    w.store.create_items(w.p("router"), [
        defn("T", kind="operator_task", operator=None), defn("G2", ["T"], kind="goal")])
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"] if i["key"] == "demo:T")
    assert row["kind"] == "operator_task" and row["wait"] == "task"
    assert row["operator"] is True or row["operator"] == 1


def _write(path, items):
    path.write_text(yaml.safe_dump({"items": items}), encoding="utf-8")


def test_importer_kinds_and_defaults(tmp_path):
    _write(tmp_path / "x.yaml", [
        {"project": "demo", "id": "Q", "kind": "question"},
        {"project": "demo", "id": "T", "kind": "operator_task", "operator": False},
        {"project": "demo", "id": "N", "kind": "decision", "wait": None},
        {"project": "demo", "id": "L"}])
    got = {i["id"]: i for i in importer.load_dir(tmp_path)}
    assert (got["Q"]["operator"], got["Q"]["wait"]) == (True, "answer")
    assert (got["T"]["operator"], got["T"]["wait"]) == (False, "task")
    assert (got["N"]["operator"], got["N"]["wait"]) == (True, None)
    assert (got["L"]["operator"], got["L"]["wait"]) == (False, None)


def test_importer_unknown_kind(tmp_path):
    _write(tmp_path / "x.yaml", [{"project": "demo", "id": "Z", "kind": "epic"}])
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert "kind" in exc.value.msg


def test_export_import_round_trip_keeps_explicit_null_wait(w, tmp_path):
    w.store.create_items(w.p("router"), [
        defn("Q", kind="question", operator=None),
        defn("N", kind="decision", wait=None, operator=None),
        defn("G2", ["Q", "N"], kind="goal")])
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    got = {i["key"]: i for i in importer.load_dir(out)}
    assert got["demo:Q"]["wait"] == "answer" and got["demo:N"]["wait"] is None
    assert got["demo:Q"]["kind"] == "question"
