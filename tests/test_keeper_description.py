"""MODEL-NAMES: ``description`` is a first-class item column, not folded text."""
import sqlite3

import pytest
import yaml

from keeper_util import World, defn, seed, to_done
from plangraph.keeper import db as kdb, importer
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


def test_create_stores_description_in_the_column_not_the_note(w):
    w.store.create_items(w.p("router"), [
        defn("X", description="what X is for", note="hand written"),
        defn("G2", ["X"], kind="goal")])
    item = _item(w, "demo:X")
    assert item["description"] == "what X is for"
    assert item["note"] == "hand written"


def test_create_without_description_leaves_it_null(w):
    assert _item(w, "demo:A")["description"] is None


def test_other_unknown_fields_still_fold_into_the_note(w):
    w.store.create_items(w.p("router"), [
        defn("X", description="d", intent="why"),
        defn("G2", ["X"], kind="goal")])
    item = _item(w, "demo:X")
    assert item["description"] == "d" and item["note"] == "intent: why"


@pytest.mark.parametrize("value", [5, ["a"], {"a": 1}])
def test_non_string_description_refused(w, value):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", description=value)])
    assert exc.value.code == "validation" and "description" in exc.value.msg


def test_description_cap_is_2000(w):
    w.store.create_items(w.p("router"), [
        defn("X", description="d" * 2000), defn("G2", ["X"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("Y", description="d" * 2001)])
    assert "description" in exc.value.msg and "2000" in exc.value.msg
    assert _item(w, "demo:Y") is None


def test_edit_sets_description_and_integrity_agrees(w):
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"description": "now described"})
    assert _item(w, "demo:A")["description"] == "now described"
    assert verify_integrity(w.conn) == []


def test_create_integrity_agrees(w):
    w.store.create_items(w.p("router"), [
        defn("X", description="d"), defn("G2", ["X"], kind="goal")])
    assert verify_integrity(w.conn) == []


def test_tampered_description_is_an_integrity_problem(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "real"})
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET description='forged' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    problems = verify_integrity(w.conn)
    assert [(p["key"], p["field"], p["expected"], p["got"]) for p in problems] \
        == [("demo:A", "description", "real", "forged")]


def test_edit_description_credential_guarded(w):
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A",
                                {"description": "login admin/Zq9xk2mQ7w"})
    assert "description" in exc.value.msg and "Zq9xk2" not in exc.value.msg


def test_edit_description_via_apply_ops(w):
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"description": "via ops"}}])
    assert _item(w, "demo:A")["description"] == "via ops"
    assert verify_integrity(w.conn) == []


def test_show_and_snapshot_carry_description(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "shown"})
    assert w.store.show(w.p("reader"), "demo:A")["description"] == "shown"
    snap = w.store.snapshot(w.p("reader"))
    row = next(i for i in snap["items"] if i["key"] == "demo:A")
    assert row["description"] == "shown"


def test_export_import_round_trip_keeps_description(w, tmp_path):
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "kept"})
    out = tmp_path / "plan"
    importer.export_yaml(w.store, out)
    items = {i["key"]: i for i in importer.load_dir(out)}
    assert items["demo:A"]["description"] == "kept"
    assert "description" not in (items["demo:A"].get("note") or "")
    again = tmp_path / "plan2"
    importer.export_yaml(w.store, again)
    assert importer.load_dir(again) == importer.load_dir(out)


def test_importer_description_goes_to_the_column(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "title": "t", "track": "T0",
        "owner": "o", "description": "imp", "note": "n"}]}), encoding="utf-8")
    (item,) = importer.load_dir(tmp_path)
    assert item["description"] == "imp" and item["note"] == "n"


def test_importer_description_cap(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "title": "t", "track": "T0",
        "owner": "o", "description": "d" * 2001}]}), encoding="utf-8")
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert "description" in exc.value.msg and "2000" in exc.value.msg


def test_import_items_honours_description(w):
    item = defn("L", description="legacy desc")
    w.store.import_items(w.p("router"), [item, defn("G2", ["L"], kind="goal")])
    assert _item(w, "demo:L")["description"] == "legacy desc"
    assert verify_integrity(w.conn) == []


def test_existing_database_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    conn = kdb.open_db(path, create=True)
    conn.close()
    raw = sqlite3.connect(str(path))
    raw.execute("ALTER TABLE items DROP COLUMN description")
    raw.commit()
    raw.close()
    conn = kdb.open_db(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "description" in cols
    conn.close()
    kdb.open_db(path).close()  # idempotent


# ------------------------------------------------------------ migration

def _fold_note(w, key, note):
    """Put a B3-style folded note on an item, as old creates did."""
    w.conn.execute("UPDATE items SET note=? WHERE key=?", (note, key))


def test_migrate_moves_the_folded_line_to_the_column(w):
    _fold_note(w, "demo:A", "description: the folded text\nintent: why")
    result = w.store.migrate_descriptions(w.p("router"))
    assert result["migrated"] == ["demo:A"]
    item = _item(w, "demo:A")
    assert item["description"] == "the folded text"
    assert item["note"] == "intent: why"
    assert verify_integrity(w.conn) == []


def test_migrate_note_only_description_leaves_null_note(w):
    _fold_note(w, "demo:A", "description: only this")
    w.store.migrate_descriptions(w.p("router"))
    item = _item(w, "demo:A")
    assert item["description"] == "only this" and item["note"] is None


def test_migrate_is_idempotent_and_emits_one_event_per_item(w):
    _fold_note(w, "demo:A", "description: once")
    first = w.store.migrate_descriptions(w.p("router"))
    second = w.store.migrate_descriptions(w.p("router"))
    assert first["migrated"] == ["demo:A"] and second["migrated"] == []
    events = [e for e in w.conn.execute(
        "SELECT actor,type,payload FROM events WHERE key='demo:A'"
        " AND type='item_defined'")]
    assert len(events) == 1 and events[0]["actor"] == "router"


def test_migrate_skips_done_items(w):
    w.store.create_items(w.p("router"), [
        defn("D"), defn("G3", ["D"], kind="goal")])
    _fold_note(w, "demo:D", "description: locked text")
    to_done(w, "demo:D")
    # the lock trigger froze the row; its note still has the line
    note = _item(w, "demo:D")["note"]
    result = w.store.migrate_descriptions(w.p("router"))
    assert "demo:D" not in result["migrated"]
    assert _item(w, "demo:D")["note"] == note
    assert _item(w, "demo:D")["description"] is None


def test_migrate_never_overwrites_an_existing_description(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "real"})
    _fold_note(w, "demo:A", "description: stale")
    result = w.store.migrate_descriptions(w.p("router"))
    assert result["migrated"] == []
    assert _item(w, "demo:A")["description"] == "real"


def test_migrate_ignores_notes_without_a_leading_description_line(w):
    _fold_note(w, "demo:A", "hand written\ndescription: not at the start")
    assert w.store.migrate_descriptions(w.p("router"))["migrated"] == []


def test_migrate_is_router_only(w):
    for who in ("l1main", "workerA", "reader"):
        with pytest.raises(Refused) as exc:
            w.store.migrate_descriptions(w.p(who))
        assert exc.value.code == "forbidden"


# -------------------------------------------------------------- warnings

def test_alerts_warn_for_id_only_titles_and_missing_descriptions(w):
    w.store.create_items(w.p("router"), [
        defn("X", title="X-1", description="has one"),
        defn("G2", ["X"], kind="goal", title="Named goal",
             description="goal")])
    warnings = w.store.alerts(w.p("reader"))["warnings"]
    got = {(row["code"], row["key"]) for row in warnings}
    assert ("title_is_id", "demo:X") in got
    assert ("no_description", "demo:A") in got
    assert ("no_description", "demo:X") not in got
    assert ("title_is_id", "demo:G2") not in got


def test_warnings_never_block_a_write(w):
    w.store.create_items(w.p("router"), [
        defn("X", title="X-1"), defn("G2", ["X"], kind="goal")])
    assert _item(w, "demo:X") is not None


def test_alerts_text_lists_the_warnings(w):
    from plangraph.keeper import fmt
    text = fmt.alerts_text(w.store.alerts(w.p("reader")))
    assert "warning no_description demo:A" in text


def test_done_items_are_not_warned_about(w):
    to_done(w, "demo:A")
    codes = {(r["code"], r["key"]) for r in
             w.store.alerts(w.p("reader"))["warnings"]}
    assert ("no_description", "demo:A") not in codes


# ----------------------------------------------------------------- doors

@pytest.fixture
def doors(w, tmp_path):
    from door_util import Doors
    d = Doors(w, tmp_path)
    yield d
    d.close()


def test_api_create_show_and_migrate(w, doors):
    status, _ = doors.http("router", "POST", "/v1/create_items", {"items": [
        defn("X", description="over http"), defn("G2", ["X"], kind="goal")]})
    assert status == 200
    status, info = doors.http("reader", "GET", "/v1/show?key=demo:X&fmt=json")
    assert status == 200 and info["description"] == "over http"
    _fold_note(w, "demo:A", "description: folded")
    status, res = doors.http("l1main", "POST", "/v1/migrate_descriptions", {})
    assert status == 403
    status, res = doors.http("router", "POST", "/v1/migrate_descriptions", {})
    assert status == 200 and res["migrated"] == ["demo:A"]


def test_cli_show_alerts_and_migrate(w, doors):
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "cli shows"})
    code, out, _ = doors.pg("reader", "show", "demo:A")
    assert code == 0 and "desc cli shows" in out
    code, out, _ = doors.pg("reader", "alerts")
    assert code == 0 and "warning no_description demo:GOAL" in out
    _fold_note(w, "demo:GOAL", "description: g")
    code, out, _ = doors.pg("router", "migrate-descriptions")
    assert code == 0 and "migrated 1" in out
    assert _item(w, "demo:GOAL")["description"] == "g"
