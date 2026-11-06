"""S1a compatibility and read-only snapshot filters."""
import json

import pytest

from keeper_util import World, seed, to_done
from test_keeper_snapshot_setbased import _Counting, _legacy_snapshot
from plangraph.keeper.errors import Refused
from plangraph.keeper.snapshot_filter import BLOCKS, FIELDS, PARAMS


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, backend="sqlite")
    keys = seed(world, [("A", []), ("B", [])])
    to_done(world, keys[1])
    yield world
    world.close()


def test_legacy_bytes_constant_queries(w):
    store, now = w.store, w.clock()
    assert json.dumps(store._snapshot(now)) == json.dumps(_legacy_snapshot(store, now))
    assert set(FIELDS) == set(store._snapshot(now)["items"][0])
    original = store.conn
    store.conn = counter = _Counting(original)
    try:
        store._snapshot(now)
        count = counter.n
        seed(w, [("C", []), ("D", [])], project="other")
        counter.n = 0
        store._snapshot(now)
        assert counter.n == count
    finally:
        store.conn = original


def test_selection_fields_echo(w):
    body = w.store.snapshot(w.p("reader"), open="1", fields="title")
    assert len(body["items"]) == 2
    assert set(body["items"][0]) == {"key", "title"}
    assert body["filter"] == dict(open=1, closed=None, since=None,
                                   fields=["title"], blocks=None, total=2, returned=2)
    assert [i["state"] for i in w.store._snapshot(w.clock(), closed=1)["items"]] == ["done"]
    full = w.store._snapshot(w.clock())
    for name in ("aliases", "progress", "decision_text"):
        assert body[name] == full[name]


def test_activity_blocks(w):
    full = w.store._snapshot(w.clock())
    body = w.store._snapshot(w.clock(), open=1, blocks="activity")
    assert body["activity"] == full["activity"]
    closed_key = next(i["key"] for i in full["items"] if i["state"] == "done")
    assert any(e["key"] == closed_key for e in body["activity"])
    assert body["filter"]["total"] == 2
    assert body["filter"]["returned"] == 0
    assert set(body) == {"generated", "head", "head_seq", "activity", "filter"}
    for block in BLOCKS:
        assert w.store._snapshot(w.clock(), blocks=block)[block] == full[block]


def test_since_instants_done_max(w, monkeypatch):
    stamp = w.store._iso(w.clock())
    assert len(w.store._snapshot(w.clock(), since=stamp)["items"]) == 3
    body = w.store._snapshot(w.clock(), since=stamp[:-1] + ".5Z")
    assert body["items"] == [] and body["activity"] == []
    items = w.store._all_items()
    for item in items:
        item["updated_at"] = "2000-01-01T00:00:00Z"
    monkeypatch.setattr(w.store, "_all_items", lambda: items)
    body = w.store._snapshot(w.clock(), since=stamp)
    assert [i["state"] for i in body["items"]] == ["done"]


@pytest.mark.parametrize("name", PARAMS)
def test_blank_omitted(w, name):
    assert w.store._snapshot(w.clock(), **{name: ""}) == w.store._snapshot(w.clock())


@pytest.mark.parametrize("params", [
    {"open": "sentinel"}, {"closed": "sentinel"}, {"since": "sentinel"},
    {"fields": "sentinel"}, {"blocks": "sentinel"}, {"open": 1, "closed": 1},
    {"fields": "key,key"}, {"blocks": "items,items"}, {"open": "0"},
    {"closed": True}, {"since": "2024-02-30T00:00:00Z"},
    {"since": "2024-01-01T00:00:00+00:00"},
    {"since": "2024-01-01t00:00:00z"},
    {"since": "2024-01-01T00:00:00.1234567Z"},
])
def test_validation_private(w, params, capsys):
    with pytest.raises(Refused) as exc:
        w.store.snapshot(w.p("reader"), **params)
    assert exc.value.code == "validation"
    assert "sentinel" not in str(exc.value)
    assert "sentinel" not in capsys.readouterr().err
    for name in ("fields", "blocks"):
        if name in params:
            assert "allowed:" in exc.value.msg


def test_later_full_snapshot_unchanged(w):
    before = w.store._snapshot(w.clock())
    w.store._snapshot(w.clock(), closed=1, fields="key", blocks="items")
    assert w.store._snapshot(w.clock()) == before
