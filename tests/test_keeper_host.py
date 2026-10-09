"""MODEL-HOST: ``host`` is a nullable first-class item field, shown like description."""
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


def test_create_stores_host_and_default_is_null(w):
    # ws-01: synthetic host value (no domain suffix), replaces a flagged example
    w.store.create_items(w.p("router"), [
        defn("X", host="ws-01"), defn("G2", ["X"], kind="goal")])
    assert _item(w, "demo:X")["host"] == "ws-01"
    assert _item(w, "demo:A")["host"] is None
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("bad", ["Work", "a b", "", "a/b", "x" * 101, 5, ["a"]])
def test_bad_host_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", host=bad)])
    assert exc.value.code == "validation" and "host" in exc.value.msg
    assert _item(w, "demo:X") is None


def test_edit_host_and_integrity(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"host": "example-host"})
    assert _item(w, "demo:A")["host"] == "example-host"
    assert verify_integrity(w.conn) == []
    w.store.edit_definition(w.p("router"), "demo:A", {"host": None})
    assert _item(w, "demo:A")["host"] is None
    assert verify_integrity(w.conn) == []


def test_edit_bad_host_refused(w):
    with pytest.raises(Refused):
        w.store.edit_definition(w.p("router"), "demo:A", {"host": "Bad Host"})


def test_tampered_host_is_an_integrity_problem(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"host": "real"})
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET host='forged' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    assert [(p["field"], p["expected"], p["got"])
            for p in verify_integrity(w.conn)] == [("host", "real", "forged")]


def test_show_snapshot_and_text_carry_host(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"host": "h1"})
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["host"] == "h1" and "host h1" in fmt.show_text(info)
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"]
               if i["key"] == "demo:A")
    assert row["host"] == "h1"


def test_export_import_round_trip(w, tmp_path):
    w.store.edit_definition(w.p("router"), "demo:A", {"host": "h1"})
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    items = {i["key"]: i for i in importer.load_dir(out)}
    assert items["demo:A"]["host"] == "h1" and items["demo:GOAL"]["host"] is None
    assert "host" not in (items["demo:A"].get("note") or "")


def test_importer_validates_host(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "title": "t", "host": "Bad Host"}]}),
        encoding="utf-8")
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert "host" in exc.value.msg


def test_import_items_honours_host(w):
    w.store.import_items(w.p("router"), [
        defn("L", host="legacy-box"), defn("G2", ["L"], kind="goal")])
    assert _item(w, "demo:L")["host"] == "legacy-box"
    assert verify_integrity(w.conn) == []


def test_existing_database_gains_host_and_aliases(tmp_path):
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE items DROP COLUMN host")
    raw.execute("DROP TABLE aliases")
    raw.commit()
    raw.close()
    for _ in range(2):  # the second open is a no-op
        conn = kdb.open_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "host" in cols and "aliases" in tables
        with pytest.raises(sqlite3.DatabaseError):  # append-only trigger came back
            conn.execute("DELETE FROM aliases")
            conn.execute("INSERT INTO aliases(old_key,new_key) VALUES('a:b','c:d')")
            conn.execute("DELETE FROM aliases")
        conn.close()
