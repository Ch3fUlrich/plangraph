"""B3: the create path folds unknown item fields into ``note`` like the importer."""
import pytest
import yaml

from keeper_util import World, defn, seed
from plangraph.keeper import importer
from plangraph.keeper.errors import Refused

EXTRA = {"intent": "ship the thing",
         "workstreams": ["alpha", "beta"], "budget": {"h": 3, "usd": 0}}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def _imported_note(tmp_path, item):
    (tmp_path / "plan").mkdir(exist_ok=True)
    (tmp_path / "plan" / "x.yaml").write_text(
        yaml.safe_dump({"items": [item]}), encoding="utf-8")
    (loaded,) = importer.load_dir(tmp_path / "plan")
    return loaded["note"]


def _note(w, key):
    return w.store._item(key)["note"]


def test_create_folds_unknown_fields_like_an_import(w, tmp_path):
    item = defn("X", **EXTRA)
    expected = _imported_note(tmp_path, item)
    assert "intent: ship the thing" in expected
    w.store.create_items(w.p("router"), [item,
                         defn("GOAL2", ["X"], kind="goal")])
    assert _note(w, "demo:X") == expected


def test_create_keeps_explicit_note_then_folds(w, tmp_path):
    item = defn("X", note="hand written", intent="why")
    w.store.create_items(w.p("router"), [item, defn("G2", ["X"], kind="goal")])
    assert _note(w, "demo:X") == _imported_note(tmp_path, item)
    assert _note(w, "demo:X").startswith("hand written\nintent: why")


def test_apply_ops_create_folds_too(w, tmp_path):
    item = defn("X", intent="via a batch")
    w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": item},
        {"op": "edit", "key": "demo:GOAL", "fields": {"depends_on": ["A", "X"]}}])
    assert _note(w, "demo:X") == "intent: via a batch"


def test_oversize_fold_refused_naming_the_field(w):
    before = w.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("X", intent="d" * 2100), defn("G2", ["X"], kind="goal")])
    assert exc.value.code == "validation"
    assert "intent" in exc.value.msg and "2000" in exc.value.msg
    assert w.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before


def test_oversize_from_note_plus_fold_refused(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("X", note="n" * 1990, intent="i" * 40),
            defn("G2", ["X"], kind="goal")])
    assert exc.value.code == "validation" and "intent" in exc.value.msg


def test_known_fields_unchanged(w):
    w.store.create_items(w.p("router"), [
        defn("X", note="plain", owner_family="google", priority=2),
        defn("G2", ["X"], kind="goal")])
    item = w.store._item("demo:X")
    assert item["note"] == "plain" and item["owner_family"] == "google"
    assert item["priority"] == 2
    w.store.create_items(w.p("router"), [defn("Y"), defn("G3", ["Y"],
                                                          kind="goal")])
    assert _note(w, "demo:Y") is None


def test_non_string_note_still_refused(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", note=5, intent="i")])
    assert exc.value.code == "validation"


def test_importer_behaviour_unchanged(tmp_path):
    note = _imported_note(tmp_path, {"project": "demo", "id": "Z", "title": "t",
                                     "track": "T0", "owner": "o", "note": "n",
                                     "zeta": 1, "alpha": "a"})
    assert note == "n\nalpha: a\nzeta: 1"
