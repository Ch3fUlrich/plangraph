"""MK-FIELDS: optional item fields layer/role/agent_id/phase_kind, like ``wait``.

Mutation killed by these tests: dropping the four names from ``SMALL_FIELDS``
(events fold / ``_defined_payload``) makes ``test_edit_and_integrity`` fail with
a ``cache_mismatch``; dropping the gate check in ``check_model_fields`` makes
``test_phase_kind_gate_only`` fail.
"""
import sqlite3

import pytest
import yaml

from keeper_util import World, defn, seed
from plangraph.keeper import db as kdb, fmt, importer
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

NEW = ("layer", "role", "agent_id", "phase_kind")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def _item(w, key):
    return w.store._item(key)


def test_create_with_fields_and_default_null(w):
    w.store.create_items(w.p("router"), [
        defn("X", layer="L2", role="implementer", agent_id="plangraph-L2-mk:1@vm"),
        defn("GT", kind="gate", phase_kind="planning"),
        defn("G2", ["X", "GT"], kind="goal")])
    x = _item(w, "demo:X")
    assert (x["layer"], x["role"], x["agent_id"], x["phase_kind"]) == (
        "L2", "implementer", "plangraph-L2-mk:1@vm", None)
    assert _item(w, "demo:GT")["phase_kind"] == "planning"
    assert all(_item(w, "demo:A")[n] is None for n in NEW)
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("field,bad", [
    ("layer", "L4"), ("layer", "l1"), ("layer", 1), ("layer", ""),
    ("role", "boss"), ("role", "Planner"), ("role", ["planner"]),
    ("agent_id", "has space"), ("agent_id", "x" * 129), ("agent_id", ""),
    ("agent_id", 7), ("agent_id", "a/b"),
    ("phase_kind", "later"), ("phase_kind", True)])
def test_bad_value_refused(w, field, bad):
    kind = "gate" if field == "phase_kind" else "lane"
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", kind=kind, **{field: bad})])
    assert exc.value.code == "validation" and field in exc.value.msg
    assert _item(w, "demo:X") is None


def test_agent_id_cap_boundary(w):
    w.store.create_items(w.p("router"), [defn("X", agent_id="a" * 128),
                                         defn("G2", ["X"], kind="goal")])
    assert len(_item(w, "demo:X")["agent_id"]) == 128


@pytest.mark.parametrize("kind", ["lane", "goal"])
def test_phase_kind_gate_only(w, kind):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", kind=kind, phase_kind="work")])
    assert exc.value.code == "validation" and "gate" in exc.value.msg
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), "demo:A", {"phase_kind": "work"})
    # null is always fine
    w.store.create_items(w.p("router"), [defn("Y", kind=kind, phase_kind=None),
                                         defn("G2", ["Y"], kind="goal")])


def test_edit_and_integrity(w):
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"layer": "L3", "role": "reviewer", "agent_id": "r1"})
    a = _item(w, "demo:A")
    assert (a["layer"], a["role"], a["agent_id"]) == ("L3", "reviewer", "r1")
    assert verify_integrity(w.conn) == []
    w.store.edit_definition(w.p("router"), "demo:A", {"layer": None, "role": None})
    assert _item(w, "demo:A")["layer"] is None
    assert verify_integrity(w.conn) == []
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"role": "boss"})
    assert exc.value.code == "validation" and "role" in exc.value.msg


def test_edit_phase_kind_on_gate_and_batch(w):
    w.store.create_items(w.p("router"), [defn("GT", kind="gate"),
                                         defn("G2", ["GT"], kind="goal")])
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GT", "fields": {"phase_kind": "work"}}])
    assert _item(w, "demo:GT")["phase_kind"] == "work"
    assert verify_integrity(w.conn) == []


def test_show_snapshot_and_text(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"layer": "L1", "role": "planner"})
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["layer"] == "L1" and info["role"] == "planner"
    assert info["agent_id"] is None and info["phase_kind"] is None
    assert "layer L1 role planner" in fmt.show_text(info)
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"] if i["key"] == "demo:A")
    assert row["layer"] == "L1" and row["role"] == "planner"


def test_tampered_field_is_an_integrity_problem(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"role": "reviewer"})
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET role='planner', layer='L1' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    got = sorted((p["field"], p["expected"], p["got"]) for p in verify_integrity(w.conn))
    assert got == [("layer", None, "L1"), ("role", "reviewer", "planner")]


def test_export_import_round_trip(w, tmp_path):
    w.store.create_items(w.p("router"), [
        defn("GT", kind="gate", phase_kind="work", layer="L2", agent_id="g"),
        defn("G2", ["GT"], kind="goal")])
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    items = {i["key"]: i for i in importer.load_dir(out)}
    assert items["demo:GT"]["phase_kind"] == "work" and items["demo:GT"]["layer"] == "L2"
    assert items["demo:A"]["layer"] is None


def test_importer_validates(tmp_path):
    for extra in ({"layer": "L9"}, {"role": "x"}, {"agent_id": "a b"},
                  {"phase_kind": "work"}):  # phase_kind on a lane
        (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
            "project": "demo", "id": "Z", "title": "t", **extra}]}), encoding="utf-8")
        with pytest.raises(Refused) as exc:
            importer.load_dir(tmp_path)
        assert next(iter(extra)) in exc.value.msg


def test_existing_database_gains_columns(tmp_path):
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    for col in NEW:
        raw.execute(f"ALTER TABLE items DROP COLUMN {col}")
    raw.commit()
    raw.close()
    for _ in range(2):
        conn = kdb.open_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        assert set(NEW) <= cols
        conn.close()
