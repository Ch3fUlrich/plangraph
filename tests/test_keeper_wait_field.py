"""D-677: optional item field ``wait`` (``answer``/``task``/null), like ``host``.

Mutation killed by these tests: dropping ``wait`` from the ``item_defined``
payload in ``store_create._defined_payload`` makes
``test_edit_wait_and_integrity`` fail (``verify_integrity`` reports a
``cache_mismatch`` on ``wait`` after an edit).
"""
import sqlite3

import pytest
import yaml

from keeper_util import World, defn, seed
from plangraph.keeper import db as kdb, fmt, importer
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


def test_create_with_wait_and_default_null(w):
    w.store.create_items(w.p("router"), [
        defn("X", wait="answer"), defn("G2", ["X"], kind="goal")])
    assert _item(w, "demo:X")["wait"] == "answer"
    assert _item(w, "demo:A")["wait"] is None
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("bad", ["soon", "ANSWER", "", 5, ["answer"], True])
def test_bad_wait_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", wait=bad)])
    assert exc.value.code == "validation" and "wait" in exc.value.msg
    assert _item(w, "demo:X") is None


def test_edit_wait_and_integrity(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": "task"})
    assert _item(w, "demo:A")["wait"] == "task"
    assert verify_integrity(w.conn) == []
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": None})
    assert _item(w, "demo:A")["wait"] is None
    assert verify_integrity(w.conn) == []
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"wait": "later"})
    assert exc.value.code == "validation" and "wait" in exc.value.msg


def test_edit_wait_via_batch(w):
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A",
         "fields": {"wait": "answer"}}])
    assert _item(w, "demo:A")["wait"] == "answer"
    assert verify_integrity(w.conn) == []


def test_show_snapshot_and_text_carry_wait(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": "answer"})
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["wait"] == "answer" and "wait answer" in fmt.show_text(info)
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"]
               if i["key"] == "demo:A")
    assert row["wait"] == "answer"
    assert row["waits_on_operator"] is False


def test_waits_on_operator_derived(w):
    w.store.create_items(w.p("router"), [
        defn("OP", operator=True), defn("G2", ["OP"], kind="goal")])
    rows = {i["key"]: i for i in w.store.snapshot(w.p("reader"))["items"]}
    assert rows["demo:OP"]["wait"] is None
    assert rows["demo:OP"]["waits_on_operator"] is True
    assert rows["demo:A"]["waits_on_operator"] is False


def test_tampered_wait_is_an_integrity_problem(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": "task"})
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET wait='forged' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    assert [(p["field"], p["expected"], p["got"])
            for p in verify_integrity(w.conn)] == [("wait", "task", "forged")]


def test_export_import_round_trip(w, tmp_path):
    w.store.edit_definition(w.p("router"), "demo:A", {"wait": "task"})
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    items = {i["key"]: i for i in importer.load_dir(out)}
    assert items["demo:A"]["wait"] == "task"
    assert items["demo:GOAL"]["wait"] is None


def test_importer_validates_wait(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "title": "t", "wait": "soon"}]}),
        encoding="utf-8")
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert "wait" in exc.value.msg


def test_existing_database_gains_wait(tmp_path):
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE items DROP COLUMN wait")
    raw.commit()
    raw.close()
    for _ in range(2):  # the second open is a no-op
        conn = kdb.open_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        assert "wait" in cols
        conn.close()
