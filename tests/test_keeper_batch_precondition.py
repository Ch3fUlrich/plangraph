"""Tests for apply_ops base_seq precondition, edit depends_on forms, and dry_run."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("C"), defn("B", ["C"]), defn("A", ["B", "C"]),
        defn("GOAL", ["A", "B", "C"], kind="goal"),
    ])
    yield world
    world.close()


def _events(w):
    return w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _max_seq(w):
    row = w.conn.execute("SELECT MAX(seq) FROM events").fetchone()
    return row[0] or 0


def _item(w, key):
    return w.store._item(key)


def test_precondition_edit_stale_key_refused(w):
    """(a) edit of item changed after base_seq is refused with conflict,
    listing all stale keys, and earlier/later batch ops are not applied."""
    base = _max_seq(w)
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "title A changed"}},
        {"op": "edit", "key": "demo:B", "fields": {"title": "title B changed"}},
    ])
    before_events = _events(w)
    c_title_before = _item(w, "demo:C")["title"]

    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "edit", "key": "demo:A", "fields": {"title": "title A batch"}},
            {"op": "edit", "key": "demo:B", "fields": {"title": "title B batch"}},
            {"op": "edit", "key": "demo:C", "fields": {"title": "title C batch"}},
        ], base_seq=base)

    assert ei.value.code == "conflict"
    assert f"base_seq {base}: changed after base_seq: demo:A, demo:B" in ei.value.msg
    assert _events(w) == before_events
    assert _item(w, "demo:C")["title"] == c_title_before
    assert _item(w, "demo:A")["title"] == "title A changed"


def test_precondition_cancel_stale_key_refused(w):
    """(b) cancel of item changed after base_seq is refused with conflict,
    listing stale key, and rollback leaves item open and events unchanged."""
    base = _max_seq(w)
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "title A changed"}},
    ])
    before_events = _events(w)

    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "cancel", "key": "demo:A", "reason": "obsolete"},
            {"op": "edit", "key": "demo:C", "fields": {"title": "title C batch"}},
        ], base_seq=base)

    assert ei.value.code == "conflict"
    assert f"base_seq {base}: changed after base_seq: demo:A" in ei.value.msg
    assert _events(w) == before_events
    assert _item(w, "demo:A")["state"] == "open"


def test_precondition_untouched_item_passes_and_zero_refuses(w):
    """(c) untouched item edits fine with current max seq; base_seq=0 refuses."""
    current = _max_seq(w)
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "title A touched"}},
    ], base_seq=current)
    assert res["applied"] == 1
    assert _item(w, "demo:A")["title"] == "title A touched"

    with pytest.raises(Refused) as ei:
        w.store.apply_ops(w.p("router"), [
            {"op": "edit", "key": "demo:A", "fields": {"title": "title A again"}},
        ], base_seq=0)
    assert ei.value.code == "conflict"
    assert "base_seq 0: changed after base_seq: demo:A" in ei.value.msg


def test_precondition_boundary_event_at_base_seq_is_not_stale(w):
    """(d1) an event AT base_seq is not stale; only seq > base_seq refuses."""
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "boundary touch"}},
    ])
    boundary = _max_seq(w)
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "after boundary"}},
    ], base_seq=boundary)
    assert res["applied"] == 1
    assert _item(w, "demo:A")["title"] == "after boundary"


def test_precondition_absent_base_seq_does_no_check(w):
    """(d) base_seq absent or None skips stale check."""
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "touch A"}},
    ])
    res1 = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "touch A again"}},
    ], base_seq=None)
    assert res1["applied"] == 1

    res2 = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:A", "fields": {"title": "touch A third"}},
    ])
    assert res2["applied"] == 1
    assert _item(w, "demo:A")["title"] == "touch A third"


def test_bad_base_seq_and_dry_run_types_refused(w):
    """(e) invalid types for base_seq and dry_run are refused with validation."""
    for bad in (-1, True, False, "1", 1.5, []):
        with pytest.raises(Refused) as ei:
            w.store.apply_ops(w.p("router"), [
                {"op": "edit", "key": "demo:A", "fields": {"title": "t"}},
            ], base_seq=bad)
        assert ei.value.code == "validation"
        assert ei.value.msg == "base_seq must be a non-negative integer"

    for bad in ("true", 1, 0, [], {}):
        with pytest.raises(Refused) as ei:
            w.store.apply_ops(w.p("router"), [
                {"op": "edit", "key": "demo:A", "fields": {"title": "t"}},
            ], dry_run=bad)
        assert ei.value.code == "validation"
        assert "dry_run must be a boolean" in ei.value.msg


def test_item_created_earlier_in_batch_then_edited_not_stale(w):
    """(f) key created earlier in the same batch then edited is not stale."""
    res = w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": [defn("G2", kind="goal")]},
        {"op": "edit", "key": "demo:G2", "fields": {"title": "title G2 edited"}},
    ], base_seq=0)
    assert res["applied"] == 2
    assert _item(w, "demo:G2")["title"] == "title G2 edited"


def test_depends_on_add_remove_forms(w):
    """(g) depends_on add/remove forms: add keeps others, remove drops one,
    both together, add of present is no-op, bad shape refused, earlier op seen."""
    # 1. remove drops one
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:C"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A", "demo:B"]

    # 2. both together
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["demo:C"], "remove": ["demo:B"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A", "demo:C"]

    # 3. add keeps others
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["B"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == sorted(
        ["demo:A", "demo:C", "demo:B"])

    # 4. add of present is no-op
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"add": ["demo:B"]}}},
    ])
    assert res["applied"] == 1
    assert _item(w, "demo:GOAL")["depends_on"] == sorted(
        ["demo:A", "demo:C", "demo:B"])

    # 5. bad shapes refused
    for bad in ({"invalid": ["demo:A"]}, {"add": "demo:A"}, {"remove": 1}, {"add": [123]}):
        with pytest.raises(Refused) as ei:
            w.store.apply_ops(w.p("router"), [
                {"op": "edit", "key": "demo:GOAL",
                 "fields": {"depends_on": bad}},
            ])
        assert ei.value.code == "validation"
        assert "depends_on form takes only add/remove lists of keys" in ei.value.msg

    # 6. earlier op in same batch is seen
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:B"]}}},
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:C"]}}},
    ])
    assert res["applied"] == 2
    assert _item(w, "demo:GOAL")["depends_on"] == ["demo:A"]


def test_full_list_replace_reports_dropped_deps(w):
    """(h) full-list replace reports dropped_deps; empty when none dropped."""
    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": ["demo:A", "demo:B"]}},
    ])
    assert res["applied"] == 1
    assert res["results"][0]["dropped_deps"] == ["demo:C"]

    # When no deps are dropped, dropped_deps is absent
    res2 = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": ["demo:A", "demo:B"]}},
    ])
    assert res2["applied"] == 1
    assert "dropped_deps" not in res2["results"][0]


def test_dry_run_leaves_store_unchanged(w):
    """(i) dry_run returns results incl. dropped_deps, nothing persisted
    (events and max seq identical), and dry_run refusal matches live refusal."""
    before_events = _events(w)
    before_max_seq = _max_seq(w)
    before_goal = _item(w, "demo:GOAL")

    res = w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"remove": ["demo:C"]}}},
    ], dry_run=True)

    assert res["applied"] == 1
    assert res["dry_run"] is True
    assert res["results"][0]["dropped_deps"] == ["demo:C"]

    # Nothing persisted
    assert _events(w) == before_events
    assert _max_seq(w) == before_max_seq
    assert _item(w, "demo:GOAL") == before_goal
    assert verify_integrity(w.conn) == []

    # Dry run refusal matches live refusal
    bad_ops = [
        {"op": "edit", "key": "demo:GOAL",
         "fields": {"depends_on": {"unknown": []}}},
    ]
    with pytest.raises(Refused) as live_ei:
        w.store.apply_ops(w.p("router"), bad_ops, dry_run=False)
    with pytest.raises(Refused) as dry_ei:
        w.store.apply_ops(w.p("router"), bad_ops, dry_run=True)

    assert dry_ei.value.code == live_ei.value.code
    assert dry_ei.value.msg == live_ei.value.msg
