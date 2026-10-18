import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools"))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import apply_batch
import apply_batch_atomic as atomic
from keeper_util import defn
from test_apply_batch import world, srv, token_file, run, item_count, write_batch as _base_write_batch

try:
    from test_apply_batch import state_of, title_of
except ImportError:
    def state_of(w, key):
        p, i = key.split(":", 1)
        r = w.conn.execute("SELECT state FROM items WHERE project=? AND id=?", (p, i)).fetchone()
        return r["state"] if r else None

    def title_of(w, key):
        p, i = key.split(":", 1)
        r = w.conn.execute("SELECT title FROM items WHERE project=? AND id=?", (p, i)).fetchone()
        return r["title"] if r else None


def write_batch(tmp_path, ops=None, name="batch", tamper=False, base_seq=None, extra_top=None):
    kwargs = {"name": name, "tamper": tamper}
    if ops is not None:
        kwargs["ops"] = ops
    batch_path = _base_write_batch(tmp_path, **kwargs)
    if base_seq is not None or extra_top:
        with open(batch_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if base_seq is not None:
            data["base_seq"] = base_seq
        if extra_top:
            data.update(extra_top)
        with open(batch_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
    return batch_path


def max_event_seq(world):
    row = world.conn.execute("SELECT MAX(seq) FROM events").fetchone()
    return row[0] if row and row[0] is not None else 0


def test_base_seq_stale_edit_refuses_whole_batch(world, srv, token_file, tmp_path):
    # Setup items
    e1 = defn("E1")
    e1["title"] = "Initial E1"
    e2 = defn("E2")
    e2["title"] = "Initial E2"
    world.store.create_items(
        world.p("router"),
        [e1, e2, defn("GOAL", ["E1", "E2"], kind="goal")],
    )

    base_seq = max_event_seq(world)

    # Trigger a newer event on demo:E1
    bump_ops = [{"op": "edit", "key": "demo:E1", "fields": {"title": "Bumped E1"}}]
    b_bump = write_batch(tmp_path, ops=bump_ops, name="bump_e1")
    rc1, _, _ = run(b_bump, srv.base_url, token_file)
    assert rc1 == 0

    # Submit batch with stale base_seq modifying both E1 and E2
    stale_ops = [
        {"op": "edit", "key": "demo:E1", "fields": {"title": "Stale E1"}},
        {"op": "edit", "key": "demo:E2", "fields": {"title": "New E2"}},
    ]
    b_stale = write_batch(tmp_path, ops=stale_ops, name="stale_edit_batch", base_seq=base_seq)
    rc, out, errtxt = run(b_stale, srv.base_url, token_file)

    assert rc == 1, out + errtxt
    assert "changed after base_seq" in out
    assert "demo:E1" in out
    assert "batch X7d-stale_edit_batch: conflict" in out
    assert "nothing applied (the whole batch was rolled back)" in out
    assert "op 1 " not in out
    # Verify neither op took effect
    assert title_of(world, "demo:E1") == "Bumped E1"
    assert title_of(world, "demo:E2") == "Initial E2"


def test_base_seq_stale_cancel_refuses(world, srv, token_file, tmp_path):
    c1 = defn("C1")
    c1["title"] = "Initial C1"
    world.store.create_items(
        world.p("router"),
        [c1, defn("GOAL", ["C1"], kind="goal")],
    )

    base_seq = max_event_seq(world)

    bump_ops = [{"op": "edit", "key": "demo:C1", "fields": {"title": "Edited Before Cancel"}}]
    b_bump = write_batch(tmp_path, ops=bump_ops, name="bump_c1")
    rc1, _, _ = run(b_bump, srv.base_url, token_file)
    assert rc1 == 0

    cancel_ops = [{"op": "cancel", "key": "demo:C1", "reason": "obsolete"}]
    b_cancel = write_batch(tmp_path, ops=cancel_ops, name="stale_cancel_batch", base_seq=base_seq)
    rc, out, errtxt = run(b_cancel, srv.base_url, token_file)

    assert rc == 1, out + errtxt
    assert "changed after base_seq: demo:C1" in out
    assert "batch X7d-stale_cancel_batch: conflict" in out
    assert "nothing applied (the whole batch was rolled back)" in out
    assert state_of(world, "demo:C1") != "cancelled"


def test_fresh_base_seq_applies(world, srv, token_file, tmp_path):
    f1 = defn("F1")
    f1["title"] = "Initial F1"
    world.store.create_items(
        world.p("router"),
        [f1, defn("GOAL", ["F1"], kind="goal")],
    )

    base_seq = max_event_seq(world)

    fresh_ops = [{"op": "edit", "key": "demo:F1", "fields": {"title": "Fresh Title"}}]
    b_fresh = write_batch(tmp_path, ops=fresh_ops, name="fresh_batch", base_seq=base_seq)
    rc, out, errtxt = run(b_fresh, srv.base_url, token_file)

    assert rc == 0, out + errtxt
    assert "ok 1 edit demo:F1" in out
    assert title_of(world, "demo:F1") == "Fresh Title"


def test_bad_base_seq_values_exit_non_zero(world, srv, token_file, tmp_path):
    for bad_val in [-1, "x", True]:
        b = write_batch(tmp_path, name=f"bad_seq_{bad_val}", extra_top={"base_seq": bad_val})
        rc, out, errtxt = run(b, srv.base_url, token_file)
        assert rc != 0
        assert "base_seq must be a non-negative integer" in out + errtxt


def test_per_op_with_base_seq_exits_2(world, srv, token_file, tmp_path):
    b = write_batch(tmp_path, name="per_op_base_seq", base_seq=0)
    rc, out, errtxt = run(b, srv.base_url, token_file, extra=["--per-op"])
    assert rc == 2
    assert "error: base_seq needs the atomic mode (drop --per-op)" in out + errtxt


def test_depends_on_add_remove_and_dropped_dep_printed(world, srv, token_file, tmp_path):
    world.store.create_items(
        world.p("router"),
        [
            defn("T1"),
            defn("T2"),
            defn("T3"),
            defn("GOAL", ["T1", "T2", "T3"], kind="goal"),
        ],
    )

    add_ops = [
        {"op": "edit", "key": "demo:T1", "fields": {"depends_on": {"add": ["demo:T2", "demo:T3"]}}},
    ]
    b1 = write_batch(tmp_path, ops=add_ops, name="b_deps_add")
    rc1, out1, err1 = run(b1, srv.base_url, token_file)
    assert rc1 == 0, out1 + err1

    # Full-list replace dropping demo:T3
    replace_ops = [
        {"op": "edit", "key": "demo:T1", "fields": {"depends_on": ["demo:T2"]}},
    ]
    b2 = write_batch(tmp_path, ops=replace_ops, name="b_deps_replace")
    rc2, out2, err2 = run(b2, srv.base_url, token_file)
    assert rc2 == 0, out2 + err2
    assert "dropped dependency demo:T3 from demo:T1" in out2

    # Remove demo:T2 using remove dict
    remove_ops = [
        {"op": "edit", "key": "demo:T1", "fields": {"depends_on": {"remove": ["demo:T2"]}}},
    ]
    b3 = write_batch(tmp_path, ops=remove_ops, name="b_deps_remove")
    rc3, out3, err3 = run(b3, srv.base_url, token_file)
    assert rc3 == 0, out3 + err3


def test_validate_op_rejects_bad_depends_on_dict_shapes():
    bad_shapes = [
        {"foo": ["demo:B"]},
        {"add": "not-a-list"},
        {"add": [123]},
        {"add": [True]},
        {},
        123,
    ]
    for shape in bad_shapes:
        with pytest.raises(apply_batch.BatchError, match="edit: depends_on takes only add/remove lists of keys"):
            apply_batch.validate_op({"op": "edit", "key": "demo:A", "fields": {"depends_on": shape}})

    # Valid shapes must not raise
    apply_batch.validate_op({"op": "edit", "key": "demo:A", "fields": {"depends_on": {"add": ["demo:B"]}}})
    apply_batch.validate_op({"op": "edit", "key": "demo:A", "fields": {"depends_on": {"remove": ["demo:B"]}}})
    apply_batch.validate_op({"op": "edit", "key": "demo:A", "fields": {"depends_on": ["demo:B"]}})


def test_dry_run_unchanged_and_prints_note(world, srv, token_file, tmp_path):
    ops = [
        {"op": "edit", "key": "demo:B", "fields": {"depends_on": ["demo:A"]}}
    ]
    b = write_batch(tmp_path, ops=ops, name="dry_note")
    rc, out, errtxt = run(b, srv.base_url, token_file, extra=["--dry-run"])

    assert rc == 0, out + errtxt
    assert "plan 1 edit demo:B" in out
    assert "note 1 edit demo:B: depends_on list REPLACES the whole list (use {add: [...], remove: [...]} to keep others)" in out
    assert "dry run: edit 1 (no calls made)" in out
    assert item_count(world) == 0


def test_preview_prints_dropped_dep_and_writes_nothing(world, srv, token_file, tmp_path):
    p1 = defn("P1", ["P2", "P3"])
    p1["title"] = "Preview P1"
    p2 = defn("P2")
    p2["title"] = "Preview P2"
    p3 = defn("P3")
    p3["title"] = "Preview P3"
    world.store.create_items(
        world.p("router"),
        [
            p2,
            p3,
            p1,
            defn("GOAL", ["P1", "P2", "P3"], kind="goal"),
        ],
    )

    ev_count_before = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    title_before = title_of(world, "demo:P1")

    preview_ops = [
        {"op": "edit", "key": "demo:P1", "fields": {"title": "Preview Title Changed", "depends_on": ["demo:P2"]}},
    ]
    b_preview = write_batch(tmp_path, ops=preview_ops, name="preview_batch")
    rc, out, errtxt = run(b_preview, srv.base_url, token_file, extra=["--dry-run", "--preview"])

    assert rc == 0, out + errtxt
    assert "dropped dependency demo:P3 from demo:P1" in out
    assert "preview: nothing applied (dry run on the keeper)" in out
    assert "applied 1 of" not in out

    ev_count_after = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert ev_count_after == ev_count_before
    assert title_of(world, "demo:P1") == title_before


def test_preview_without_dry_run_exits_2(world, srv, token_file, tmp_path):
    b = write_batch(tmp_path, name="preview_no_dry")
    rc, out, errtxt = run(b, srv.base_url, token_file, extra=["--preview"])
    assert rc == 2
    assert "error: --preview needs --dry-run" in out + errtxt


def test_preview_with_per_op_exits_2(world, srv, token_file, tmp_path):
    b = write_batch(tmp_path, name="preview_per_op")
    rc, out, errtxt = run(b, srv.base_url, token_file, extra=["--dry-run", "--preview", "--per-op"])
    assert rc == 2
    assert "error: --preview needs the atomic mode (drop --per-op)" in out + errtxt
