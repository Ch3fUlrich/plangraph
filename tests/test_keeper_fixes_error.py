"""MK-FIXES-ERROR: item field ``fixes`` (max 2 live fixers per target) and the
``error`` evidence kind (unverified compressed error record).

Mutation killed by these tests: raising ``FIXES_MAX`` to 3 fails
``test_third_fix_refused``; making ``ErrorRecordVerifier`` return ``ok=True``
fails ``test_error_evidence_never_counts``.
"""
import sqlite3

import pytest
import yaml

from keeper_util import World, defn, pass_review, seed, to_claimed
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


def _fixer(w, name, target="A", **kw):
    w.store.create_items(w.p("router"), [
        defn(name, fixes=target, **kw), defn("G" + name, [name], kind="goal")])


def test_create_fixes_qualified_and_integrity(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("G2", ["F1"], kind="goal")])
    assert _item(w, "demo:F1")["fixes"] == "demo:A"
    assert _item(w, "demo:A")["fixes"] is None
    assert verify_integrity(w.conn) == []


def test_fixes_in_same_batch_as_target(w):
    w.store.create_items(w.p("router"), [
        defn("T"), defn("F", fixes="T"), defn("G2", ["T", "F"], kind="goal")])
    assert _item(w, "demo:F")["fixes"] == "demo:T"


def test_third_fix_refused(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("F2", fixes="A"),
        defn("G2", ["F1", "F2"], kind="goal")])
    with pytest.raises(Refused) as exc:
        _fixer(w, "F3")
    assert exc.value.code == "validation" and "fix" in exc.value.msg
    assert _item(w, "demo:F3") is None


def test_third_fix_in_one_batch_refused(w):
    with pytest.raises(Refused):
        w.store.create_items(w.p("router"), [
            defn("F1", fixes="A"), defn("F2", fixes="A"), defn("F3", fixes="A"),
            defn("G2", ["F1", "F2", "F3"], kind="goal")])


def test_cancelled_fix_frees_a_slot(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("F2", fixes="A"),
        defn("G2", ["F1", "F2"], kind="goal")])
    w.store.cancel(w.p("router"), "demo:F1", "dead end")
    _fixer(w, "F3")
    assert _item(w, "demo:F3")["fixes"] == "demo:A"


def test_done_fix_still_counts(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("F2", fixes="A"),
        defn("G2", ["F1", "F2"], kind="goal")])
    epoch = to_claimed(w, "demo:F1")
    w.store.add_evidence(w.p("l1main"), "demo:F1", epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), "demo:F1", epoch)
    pass_review(w, "demo:F1")
    assert _item(w, "demo:F1")["state"] == "done"
    with pytest.raises(Refused):
        _fixer(w, "F3")


@pytest.mark.parametrize("bad", ["nope", "demo:F", "", 5, ["A"]])
def test_bad_fixes_target_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("F", fixes=bad)])
    assert exc.value.code == "validation" and "fixes" in exc.value.msg
    assert _item(w, "demo:F") is None


def test_edit_fixes_and_limit(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("F2", fixes="A"), defn("X"),
        defn("G2", ["F1", "F2", "X"], kind="goal")])
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), "demo:X", {"fixes": "A"})
    w.store.edit_definition(w.p("router"), "demo:X", {"fixes": "F1"})
    assert _item(w, "demo:X")["fixes"] == "demo:F1"
    assert verify_integrity(w.conn) == []
    w.store.edit_definition(w.p("router"), "demo:X", {"fixes": None})
    assert _item(w, "demo:X")["fixes"] is None
    assert verify_integrity(w.conn) == []
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), "demo:X", {"fixes": "X"})


def test_show_snapshot_text(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("G2", ["F1"], kind="goal")])
    a = w.store.show(w.p("reader"), "demo:A")
    f = w.store.show(w.p("reader"), "demo:F1")
    assert a["fixed_by"] == ["demo:F1"] and a["fixes"] is None
    assert f["fixes"] == "demo:A" and f["fixed_by"] == []
    assert "fixes demo:A" in fmt.show_text(f)
    rows = {i["key"]: i for i in w.store.snapshot(w.p("reader"))["items"]}
    assert rows["demo:A"]["fixed_by"] == ["demo:F1"]
    assert rows["demo:F1"]["fixes"] == "demo:A"


def test_tampered_fixes_is_an_integrity_problem(w):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("G2", ["F1"], kind="goal")])
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET fixes=NULL WHERE key='demo:F1'")
    raw.commit()
    raw.close()
    assert [(p["field"], p["expected"], p["got"])
            for p in verify_integrity(w.conn)] == [("fixes", "demo:A", None)]


def test_export_import_round_trip(w, tmp_path):
    w.store.create_items(w.p("router"), [
        defn("F1", fixes="A"), defn("G2", ["F1"], kind="goal")])
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    items = {i["key"]: i for i in importer.load_dir(out)}
    assert items["demo:F1"]["fixes"] == "demo:A" and items["demo:A"]["fixes"] is None


def test_importer_validates_fixes(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "fixes": 5}]}), encoding="utf-8")
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert "fixes" in exc.value.msg


def test_existing_database_gains_fixes(tmp_path):
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE items DROP COLUMN fixes")
    raw.commit()
    raw.close()
    for _ in range(2):
        conn = kdb.open_db(path)
        assert "fixes" in {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        conn.close()


# ----------------------------------------------------------------- error evidence
GOOD = {"code": "E_TIMEOUT", "msg": "pytest timed out after 600s",
        "run_id": "run-1", "where": "tests/test_x.py::test_y"}


def test_error_evidence_recorded_unverified(w):
    epoch = to_claimed(w, "demo:A")
    out = w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error", GOOD)
    assert out["verified"] is False and out["kind"] == "error"
    assert out["ref"] == GOOD
    info = w.store.show(w.p("reader"), "demo:A")
    assert [(e["kind"], e["verified"]) for e in info["evidence"]] == [("error", False)]
    assert verify_integrity(w.conn) == []


def test_error_evidence_minimal(w):
    epoch = to_claimed(w, "demo:A")
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error",
                         {"code": "E", "msg": "m"})


def test_error_evidence_never_counts(w):
    epoch = to_claimed(w, "demo:A")
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error", GOOD)
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), "demo:A", epoch)
    assert exc.value.code == "evidence_unverified"
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"] if i["key"] == "demo:A")
    assert row["merged"] is False


@pytest.mark.parametrize("ref", [
    {}, {"code": "E"}, {"msg": "m"}, {"code": "", "msg": "m"},
    {"code": "E" * 65, "msg": "m"}, {"code": "E", "msg": "m" * 501},
    {"code": "E", "msg": "m", "extra": 1}, {"code": 1, "msg": "m"},
    {"code": "E", "msg": "m", "run_id": 5}, {"code": "E", "msg": "m", "where": "w" * 201},
    "text", None, ["E"]])
def test_bad_error_record_refused(w, ref):
    epoch = to_claimed(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error", ref)
    assert exc.value.code == "validation"
    assert w.store.show(w.p("reader"), "demo:A")["evidence"] == []


def test_error_record_boundaries_ok(w):
    epoch = to_claimed(w, "demo:A")
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error",
                         {"code": "E" * 64, "msg": "m" * 500})


def test_error_record_credential_guard(w):
    epoch = to_claimed(w, "demo:A")
    with pytest.raises(Refused):
        w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "error",
                             {"code": "E", "msg": "login admin/hunter22secret failed"})


def test_router_may_record_error_evidence(w):
    to_claimed(w, "demo:A")
    out = w.store.add_evidence_router(w.p("router"), "demo:A", "error", GOOD)
    assert out["verified"] is False
