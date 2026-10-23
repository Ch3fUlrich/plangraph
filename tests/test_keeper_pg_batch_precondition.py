"""``apply_ops`` base_seq precondition and depends_on forms on the Postgres backend.

Same contract as ``tests/test_keeper_batch_precondition.py``, driven through a
``World`` opened with ``backend="postgres"``: the stale ``base_seq`` edit/cancel
refusal (naming the stale keys, with no partial apply), the ``depends_on``
add/remove form, the refusal of a form with neither add nor remove (``{}``), and
a final event-hash / cache-integrity verification.

Needs the Postgres service of the CI ``pytest-postgres`` job
(``PG_KEEPER_BACKEND=postgres`` + ``PG_DSN``, like ``tests/test_keeper_pg_parity.py``):
every test skips -- never fails -- when ``PG_DSN`` is unset.  The file name keeps
it collected under that job (``tests/conftest.py`` skips SQLite-flavoured files).
"""
from __future__ import annotations

import os

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_chain, verify_integrity

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")

FORM_MSG = "depends_on form takes only add/remove lists of keys"


def _events(w):
    return w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _max_seq(w):
    row = w.conn.execute("SELECT MAX(seq) FROM events").fetchone()
    return row[0] or 0


def _item(w, key):
    return w.store._item(key)


def _assert_chain_ok(w):
    assert verify_chain(w.conn) == []       # every event hash recomputes
    assert verify_integrity(w.conn) == []   # cached rows == fold(events)


@pytest.fixture
def w(tmp_path):
    if not DSN:
        pytest.skip("PG_DSN not set (Postgres service container)")
    world_dir = tmp_path / "pg"
    world_dir.mkdir()
    world = World(world_dir, backend="postgres", dsn=DSN)
    world.store.create_items(world.p("router"), [
        defn("C"), defn("B", ["C"]), defn("A", ["B", "C"]),
        defn("GOAL", ["A", "B", "C"], kind="goal"),
    ])
    yield world
    world.close()


@needs_pg
def test_stale_base_seq_edit_refused_lists_keys_and_applies_nothing(w):
    """A batch whose edit touches keys changed after base_seq is refused with
    the stale item list; neither the stale nor the fresh op is applied."""
    base = _max_seq(w)
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "bumped A"}},
        {"op": "edit", "key": "demo:B", "fields": {"title": "bumped B"}},
    ])
    after_bump = _events(w)

    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "edit", "key": "demo:A", "fields": {"title": "stale A"}},
            {"op": "edit", "key": "demo:B", "fields": {"title": "stale B"}},
            {"op": "edit", "key": "demo:C", "fields": {"title": "fresh C"}},
        ], base_seq=base)

    assert ei.value.code == "conflict"
    assert f"base_seq {base}: changed after base_seq: demo:A, demo:B" in ei.value.msg
    assert _events(w) == after_bump                    # no partial apply
    assert _item(w, "demo:A")["title"] == "bumped A"
    assert _item(w, "demo:B")["title"] == "bumped B"
    assert _item(w, "demo:C")["title"] == "title C"
    assert _max_seq(w) == after_bump
    _assert_chain_ok(w)


@needs_pg
def test_stale_base_seq_cancel_refused_leaves_item_open(w):
    """A stale cancel is refused, the rest of the batch does not apply and the
    item stays open."""
    base = _max_seq(w)
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "bumped A"}},
    ])
    after_bump = _events(w)

    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "cancel", "key": "demo:A", "reason": "obsolete"},
            {"op": "edit", "key": "demo:C", "fields": {"title": "fresh C"}},
        ], base_seq=base)

    assert ei.value.code == "conflict"
    assert f"base_seq {base}: changed after base_seq: demo:A" in ei.value.msg
    assert _events(w) == after_bump
    assert _item(w, "demo:A")["state"] == "open"
    assert _item(w, "demo:C")["title"] == "title C"

    # a fresh base_seq applies the very same cancel
    res = w.store.apply_ops(w.p("router"), [
        {"op": "cancel", "key": "demo:A", "reason": "obsolete"},
        {"op": "edit", "key": "demo:C", "fields": {"title": "fresh C"}},
    ], base_seq=_max_seq(w))
    assert res["applied"] == 2
    assert _item(w, "demo:A")["state"] == "cancelled"
    _assert_chain_ok(w)


@needs_pg
def test_depends_on_add_remove_form_on_postgres(w):
    """The add/remove form edits the list in place on the Postgres backend."""
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:C"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A", "demo:B"]

    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["demo:C"], "remove": ["demo:B"]}}},
    ])
    assert res["applied"] == 1
    assert res["results"][0]["dropped_deps"] == ["demo:B"]
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A", "demo:C"]

    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["B"]}}},   # qualified to demo:B
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == sorted(
        ["demo:A", "demo:B", "demo:C"])
    _assert_chain_ok(w)


@needs_pg
def test_depends_on_empty_form_refused_no_events_written(w):
    """``{}`` (and a form with neither add nor remove) is refused with the
    form's own validation message; no event is written, items are unchanged."""
    before_events = _events(w)
    before = {k: _item(w, k) for k in ("demo:A", "demo:C", "demo:GOAL")}

    for bad in ({}, {"set": ["demo:A"]}):
        with pytest.raises(Refused) as ei:
            w.store.apply_ops(w.p("router"), [
                {"op": "edit", "key": "demo:C",
                 "fields": {"title": "title C early"}},
                {"op": "edit", "key": "demo:GOAL",
                 "fields": {"depends_on": bad}},
            ])
        assert ei.value.code == "validation"
        assert ei.value.msg == (
            f"op 2 edit demo:GOAL: validation {FORM_MSG}")
        assert _events(w) == before_events
        for key, item in before.items():
            assert _item(w, key) == item

    # the valid form still applies after the refusals
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:C"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A", "demo:B"]
    _assert_chain_ok(w)


@needs_pg
def test_event_hashes_verify_after_stale_refusals_and_deps_edits(w):
    """After the whole mix (bumps, a refused stale batch, deps edits, a
    refusal) every event hash recomputes and the cache matches the fold."""
    after_create = _events(w)
    base = _max_seq(w)
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "bumped A"}},
    ])
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), [
            {"op": "edit", "key": "demo:A", "fields": {"title": "stale"}},
            {"op": "cancel", "key": "demo:B", "reason": "obsolete"},
        ], base_seq=base)
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), [
            {"op": "edit", "key": "demo:GOAL", "fields": {"depends_on": {}}},
        ])
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["demo:B"], "remove": ["demo:C"]}}},
        {"op": "edit", "key": "demo:C", "fields": {"title": "title C again"}},
    ])
    # only the three applied edits wrote: the refusals added no event
    assert _events(w) == after_create + 3
    _assert_chain_ok(w)
