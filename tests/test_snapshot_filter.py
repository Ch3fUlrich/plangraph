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


@pytest.mark.parametrize("params", [
    {"open": 1}, {"closed": 1}, {"since": "2099-01-01T00:00:00Z"},
    {"fields": "key"}, {"open": 1, "fields": "key", "blocks": "progress"},
])
def test_progress_own_columns_all_items(w, params, monkeypatch):
    from plangraph.keeper import snapshot_filter
    expected = w.store._snapshot(w.clock())["progress"]
    original = snapshot_filter.progress

    def checked(items):
        assert len(items) == 3
        assert all(set(item) == set(snapshot_filter.PROGRESS_FIELDS) for item in items)
        return original(items)

    monkeypatch.setattr(snapshot_filter, "progress", checked)
    assert w.store._snapshot(w.clock(), **params)["progress"] == expected


def test_decision_selection_before_projection_s0(w, monkeypatch):
    from types import SimpleNamespace
    from plangraph.keeper import context_sources
    store = w.store
    full = store._snapshot(w.clock())
    evidence = {item["key"]: [{"kind": "decision", "ref": {"id": "D-" + str(n)},
                              "epoch": 0, "verified": 1, "at": item["updated_at"]}]
                for n, item in enumerate(full["items"])}
    monkeypatch.setattr(store, "_evidence_by_key", lambda: evidence)
    adapter = object()
    monkeypatch.setattr(store, "_ctx", lambda *args: SimpleNamespace(decisions=adapter))
    calls = []

    def reader(decisions, ids):
        assert decisions is adapter
        calls.append(list(ids))
        return {did: "text " + did for did in ids}

    monkeypatch.setattr(context_sources, "decision_texts", reader)
    for params in ({"open": 1}, {"closed": 1}, {"since": "2099-01-01T00:00:00Z"}, {}):
        selected = store._snapshot(w.clock(), **params)["items"]
        wanted = {evidence[item["key"]][0]["ref"]["id"] for item in selected}
        body = store._snapshot(w.clock(), fields="key", **params)
        assert body["decision_text"] == {did: "text " + did for did in wanted}
        assert all(set(item) == {"key"} for item in body["items"])
        if wanted:
            assert set(calls[-1]) == wanted
        for omitted in ("progress", "decision_text"):
            blocks = ",".join(block for block in BLOCKS if block != omitted)
            reduced = store._snapshot(w.clock(), fields="key", blocks=blocks, **params)
            assert omitted not in reduced
            assert {k: v for k, v in reduced.items() if k != "filter"} == {
                k: v for k, v in body.items() if k not in (omitted, "filter")}


def test_filtered_constant_queries(w):
    original = w.store.conn
    w.store.conn = counter = _Counting(original)
    try:
        for n, params in enumerate(({}, {"open": 1, "fields": "key"}, {"closed": 1},
                                    {"blocks": "progress,decision_text"})):
            counter.n = 0
            w.store._snapshot(w.clock(), **params)
            count = counter.n
            seed(w, [("X" + str(count), [])], project="p" + str(n))
            counter.n = 0
            w.store._snapshot(w.clock(), **params)
            assert counter.n == count
    finally:
        w.store.conn = original


def test_unexpected_param_named_private(w, capsys):
    with pytest.raises(Refused) as exc:
        w.store._snapshot(w.clock(), unexpected="sentinel")
    assert exc.value.msg == "snapshot unexpected invalid"
    assert "sentinel" not in str(exc.value)
    assert "sentinel" not in capsys.readouterr().err


@pytest.mark.parametrize("fraction", ["1", "12", "123", "1234", "12345", "123456"])
def test_fraction_normalized_for_python310(monkeypatch, fraction):
    from datetime import datetime
    from plangraph.keeper import snapshot_filter

    class Strict310:
        @staticmethod
        def fromisoformat(stamp):
            assert len(stamp.split(".")[1].split("+")[0]) == 6
            return datetime.fromisoformat(stamp)

    monkeypatch.setattr(snapshot_filter, "datetime", Strict310)
    stamp = "2026-01-01T00:00:18." + fraction + "Z"
    assert snapshot_filter._instant(stamp).microsecond == int(fraction.ljust(6, "0"))


def test_retained_imports_used_by_other_reads(w):
    # S1a LOW refuted: all four imports still serve show / progress.
    assert "choices" in w.store._show("demo:A")
    assert "kg_refs" in w.store._show("demo:A")
    assert w.store._progress_in(w.p("reader"), None, w.clock()) == w.store._snapshot(w.clock())["progress"]


def test_keeper_heading_blank_line():
    from pathlib import Path
    assert Path("docs/KEEPER.md").read_text().splitlines()[1] == ""
