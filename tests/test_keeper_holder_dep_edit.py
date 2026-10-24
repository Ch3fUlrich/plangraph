"""Holder dependency edits: authority, normalized edges and atomic audit."""
import json
from contextlib import contextmanager

import pytest

from keeper_util import World, defn, to_claimed, to_done, to_in_review
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("WORK"), defn("OP", operator=True), defn("OTHER"),
        defn("GOAL", ["WORK", "OP", "OTHER"], kind="goal"),
        defn("OP", project="other", operator=True),
        defn("GOAL", ["OP"], project="other", kind="goal")])
    yield world
    world.close()


def edit(w, deps, who="workerA", **fields):
    return w.store.edit_definition(w.p(who), "demo:WORK",
                                   {"depends_on": deps, **fields})


def test_target_refusals_do_not_disclose_existence(w):
    to_claimed(w, "demo:WORK", "workerA")
    errors = []
    for target in ("other:OP", "other:MISSING", "MISSING", "OTHER"):
        with pytest.raises(Refused) as exc:
            edit(w, [target])
        errors.append((exc.value.code, str(exc.value)))
    assert len(set(errors)) == 1


@pytest.mark.parametrize("elapsed", [7200, 7201])
def test_expired_holder_refused(w, elapsed):
    to_claimed(w, "demo:WORK", "workerA")
    w.clock.advance(elapsed)
    with pytest.raises(Refused) as exc:
        edit(w, ["OP"])
    assert exc.value.code == "forbidden"
    assert w.store._item("demo:WORK")["depends_on"] == []


@pytest.mark.parametrize("lease", [None, "invalid-date"])
def test_missing_or_unparsable_holder_lease_refused(w, lease):
    to_claimed(w, "demo:WORK", "workerA")
    w.conn.execute("UPDATE items SET lease_until=? WHERE key=?", (lease, "demo:WORK"))
    with pytest.raises(Refused, match="live claim lease"):
        edit(w, ["OP"])
    assert w.store._item("demo:WORK")["depends_on"] == []


def test_duplicates_normalized_and_resubmit_clear(w):
    epoch = to_claimed(w, "demo:WORK", "workerA")
    assert edit(w, ["OP", "demo:OP", "OP"])["epoch"] == epoch + 1
    item = w.store._item("demo:WORK")
    assert item["depends_on"] == ["demo:OP"]
    payload = json.loads(w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='item_defined' ORDER BY seq DESC",
        ("demo:WORK",)).fetchone()["payload"])
    assert payload["after"]["depends_on"] == item["depends_on"]
    with pytest.raises(Refused, match="additions only"):
        edit(w, ["OP"])
    assert w.store.verify_integrity() == []


def test_old_dependencies_are_qualified(w):
    item = {"project": "demo", "depends_on": ["OTHER"]}
    w.store._operator_dep_addition(item, ["demo:OTHER", "OP"])


@pytest.mark.parametrize("deps", [[], "OP", [None], [{"a": 1}], ["WORK"], ["MISSING"]])
def test_degenerate_inputs_clean_refusal(w, deps):
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused):
        edit(w, deps)
    assert w.store._item("demo:WORK")["depends_on"] == []


@pytest.mark.parametrize("state", ["claimed", "in_review", "done", "cancelled"])
def test_nonopen_operator_refused(w, state):
    if state == "claimed":
        to_claimed(w, "demo:OP")
    elif state == "in_review":
        to_in_review(w, "demo:OP")
    elif state == "done":
        to_done(w, "demo:OP")
    else:
        w.store.cancel(w.p("router"), "demo:OP", "not needed")
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused):
        edit(w, ["OP"])


@pytest.mark.parametrize("fields", [
    {"title": "changed"}, {"done_criteria": "changed", "review_tier": "high"},
    {"priority": 1}, {"note": "changed", "description": "changed"}])
def test_combined_payload_never_holder_authority(w, fields):
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused):
        edit(w, ["OP"], **fields)
    assert w.store._item("demo:WORK")["depends_on"] == []


def test_in_review_submitter_refused(w):
    to_in_review(w, "demo:WORK", "workerA")
    with pytest.raises(Refused):
        edit(w, ["OP"])


def test_cycle_refused(w):
    w.store.edit_definition(w.p("router"), "demo:OP", {"depends_on": ["WORK"]})
    to_claimed(w, "demo:WORK", "workerA")
    with pytest.raises(Refused):
        edit(w, ["OP"])
    assert w.store._item("demo:WORK")["depends_on"] == []


def test_router_cancelled_dep_audit_matches_storage(w):
    w.store.cancel(w.p("router"), "demo:OP", "not needed")
    to_claimed(w, "demo:WORK", "workerA")
    edit(w, ["OP", "OTHER"], who="router")
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:OTHER"]
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("change", ["lease", "target", "holder"])
def test_holder_authority_rechecked_under_write_lock(w, monkeypatch, change):
    to_claimed(w, "demo:WORK", "workerA")
    original = w.store._write

    @contextmanager
    def changed_write():
        with original():
            if change == "lease":
                w.conn.execute("UPDATE items SET lease_until=? WHERE key=?",
                               (w.store._iso(w.clock()), "demo:WORK"))
            elif change == "target":
                w.conn.execute("UPDATE items SET state='cancelled' WHERE key=?", ("demo:OP",))
            else:
                w.conn.execute("UPDATE items SET claimed_by='workerB' WHERE key=?", ("demo:WORK",))
            yield

    monkeypatch.setattr(w.store, "_write", changed_write)
    with pytest.raises(Refused):
        edit(w, ["OP"])
    assert w.store._item("demo:WORK")["depends_on"] == []


def test_router_duplicate_edges_share_normalization(w):
    to_claimed(w, "demo:WORK", "workerA")
    edit(w, ["OTHER", "demo:OTHER"], who="router")
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:OTHER"]
    assert w.store.verify_integrity() == []


def test_holder_cannot_drop_preexisting_cancelled_dependency(w):
    to_claimed(w, "demo:WORK", "workerA")
    edit(w, ["OTHER"], who="router")
    w.store.cancel(w.p("router"), "demo:OTHER", "not needed")
    with pytest.raises(Refused, match="additions only"):
        edit(w, ["OTHER", "OP"])
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:OTHER"]


@pytest.mark.parametrize("expired", [False, True])
def test_nonmember_holder_gets_scope_refusal_before_lease(w, expired):
    to_claimed(w, "demo:WORK", "workerA")
    w.conn.execute("UPDATE items SET claimed_by='reader' WHERE key=?", ("demo:WORK",))
    principal = w.p("reader")
    if expired:
        w.clock.advance(7200)
    with pytest.raises(Refused, match="not scoped to project"):
        w.store.edit_definition(principal, "demo:WORK", {"depends_on": ["OP"]})


def test_concurrent_dependency_add_is_not_overwritten(w, monkeypatch):
    to_claimed(w, "demo:WORK", "workerA")
    original = w.store._write

    @contextmanager
    def changed_write():
        monkeypatch.setattr(w.store, "_write", original)
        edit(w, ["OTHER"], who="router")
        with original():
            yield

    monkeypatch.setattr(w.store, "_write", changed_write)
    with pytest.raises(Refused, match="changed under the transaction"):
        edit(w, ["OP"])
    assert w.store._item("demo:WORK")["depends_on"] == ["demo:OTHER"]
    assert w.store.verify_integrity() == []


def test_order_only_holder_resubmit_is_clear_refusal(w):
    to_done(w, "demo:OTHER")
    w.store.edit_definition(w.p("router"), "demo:WORK", {"depends_on": ["OTHER"]})
    to_claimed(w, "demo:WORK", "workerA")
    edit(w, ["OTHER", "OP"])
    before = w.store._item("demo:WORK")
    with pytest.raises(Refused, match="additions only"):
        edit(w, ["OP", "OTHER"])
    assert w.store._item("demo:WORK") == before
    assert w.store.verify_integrity() == []


def test_legacy_null_lease_holder_must_establish_live_lease(w):
    w.store.import_items(w.p("router"), [
        defn("LEGACY", owner="workerA", state="claimed", review_tier="none",
             evidence_kinds=("decision",)),
        defn("LEGACY_GOAL", ["LEGACY"], kind="goal")])
    item = w.store._item("demo:LEGACY")
    assert item["lease_until"] is None
    with pytest.raises(Refused, match="live claim lease"):
        w.store.edit_definition(w.p("workerA"), "demo:LEGACY", {"depends_on": ["OP"]})
    w.store.release(w.p("workerA"), "demo:LEGACY", item["epoch"])
    to_claimed(w, "demo:LEGACY", "workerA")
    w.store.edit_definition(w.p("workerA"), "demo:LEGACY", {"depends_on": ["OP"]})
    assert w.store.verify_integrity() == []
