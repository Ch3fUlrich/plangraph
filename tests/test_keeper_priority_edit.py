"""PG-LE-PRIORITY-EDIT: `priority` inside an edit works like op: priority."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
from keeper_util import World, seed, to_claimed  # noqa: E402
from plangraph.keeper.errors import Refused  # noqa: E402


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    yield world
    world.close()


def _events(w, key):
    rows = w.conn.execute(
        "SELECT type, payload FROM events WHERE key=? ORDER BY seq", (key,)).fetchall()
    return [(r["type"], json.loads(r["payload"] or "{}")) for r in rows]


def _types_payloads(w, key):
    return _events(w, key)


def test_priority_only_edit_matches_op_priority(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"priority": 5})
    w.store.set_priority(w.p("router"), "demo:B", 5)
    a, b = w.store._item("demo:A"), w.store._item("demo:B")
    assert a["priority"] == b["priority"] == 5
    assert a["state"] == b["state"] == "open"
    ea = _types_payloads(w, "demo:A")[-1:]
    eb = _types_payloads(w, "demo:B")[-1:]
    assert ea == eb == [("priority_set", {"priority": 5})]
    assert [t for t, _ in _types_payloads(w, "demo:A")].count("item_defined") == 0


def test_priority_only_edit_on_claimed_behaves_like_priority(w):
    to_claimed(w, "demo:A")
    to_claimed(w, "demo:B")
    w.store.edit_definition(w.p("router"), "demo:A", {"priority": 4})
    w.store.set_priority(w.p("router"), "demo:B", 4)
    assert w.store._item("demo:A")["priority"] == 4
    assert _types_payloads(w, "demo:A")[-1:] == _types_payloads(w, "demo:B")[-1:]


def test_mixed_edit_applies_both(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"title": "new", "priority": 7})
    item = w.store._item("demo:A")
    assert item["title"] == "new"
    assert item["priority"] == 7
    tail = _types_payloads(w, "demo:A")[-2:]
    assert tail[0][0] == "item_defined"
    assert "priority" not in tail[0][1]
    assert tail[0][1]["fields"] == ["title"]
    assert tail[1] == ("priority_set", {"priority": 7})


def test_non_router_refused_with_set_priority_message(w):
    try:
        w.store.set_priority(w.p("l1main"), "demo:B", 3)
    except Refused as exc:
        expected = (exc.code, exc.msg)
    else:  # pragma: no cover
        raise AssertionError("set_priority should refuse l1")
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), "demo:A", {"priority": 3})
    assert (exc.value.code, exc.value.msg) == expected
    assert w.store._item("demo:A")["priority"] == 0
    assert _events(w, "demo:A") == before


def test_non_router_mixed_edit_refused(w):
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), "demo:A", {"title": "x", "priority": 3})
    assert exc.value.code == "forbidden"
    assert w.store._item("demo:A")["title"] != "x"
    assert w.store._item("demo:A")["priority"] == 0
    assert _events(w, "demo:A") == before


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, None])
def test_bad_priority_value_refused_no_event(w, bad):
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"priority": bad})
    assert exc.value.code == "validation"
    assert exc.value.msg == "priority must be an integer"
    assert w.store._item("demo:A")["priority"] == 0
    assert _events(w, "demo:A") == before


def test_mixed_edit_on_claimed_refused_atomic(w):
    to_claimed(w, "demo:A")
    n_before = len(_events(w, "demo:A"))
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"title": "x", "priority": 9})
    assert exc.value.code == "bad_state"
    assert w.store._item("demo:A")["priority"] == 0
    assert w.store._item("demo:A")["title"] != "x"
    assert len(_events(w, "demo:A")) == n_before


def test_apply_ops_edit_priority_rolls_back_on_later_failure(w):
    before = _events(w, "demo:A")
    ops = [{"op": "edit", "key": "demo:A", "fields": {"priority": 6}},
           {"op": "cancel", "key": "demo:NOPE", "reason": "r"}]
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), ops)
    assert w.store._item("demo:A")["priority"] == 0
    assert _events(w, "demo:A") == before


def test_apply_ops_mixed_edit_atomic_with_failing_op(w):
    before = _events(w, "demo:A")
    ops = [{"op": "edit", "key": "demo:A",
            "fields": {"title": "batched", "priority": 6}},
           {"op": "cancel", "key": "demo:NOPE", "reason": "r"}]
    with pytest.raises(Refused):
        w.store.apply_ops(w.p("router"), ops)
    assert w.store._item("demo:A")["priority"] == 0
    assert w.store._item("demo:A")["title"] != "batched"
    assert _events(w, "demo:A") == before


def test_apply_ops_edit_priority_matches_plain_verb(w):
    w.store.apply_ops(w.p("router"), [{"op": "edit", "key": "demo:A",
                                       "fields": {"priority": 8}}])
    w.store.edit_definition(w.p("router"), "demo:B", {"priority": 8})
    assert w.store._item("demo:A")["priority"] == w.store._item("demo:B")["priority"] == 8
    assert _types_payloads(w, "demo:A")[-1:] == _types_payloads(w, "demo:B")[-1:]


def test_unknown_field_still_refused_without_priority(w):
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A",
                                {"priority": 2, "bogus": 1})
    assert exc.value.code == "validation"
    assert exc.value.msg == "not editable: ['bogus']"
    assert "priority" not in exc.value.msg
    assert w.store._item("demo:A")["priority"] == 0
    assert _events(w, "demo:A") == before


def test_mutation_guard_no_priority_in_defined_payload(w):
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"title": "t", "priority": 3})
    tail = _types_payloads(w, "demo:A")[-2:]
    assert [t for t, _ in tail] == ["item_defined", "priority_set"]
    assert "priority" not in tail[0][1]
    assert tail[0][1]["fields"] == ["title"]
    assert tail[1][1] == {"priority": 3}


def test_apply_batch_client_accepts_priority_in_edit_fields():
    apply_batch.validate_op({"op": "edit", "key": "demo:A",
                             "fields": {"priority": 3}})
    verb, body = apply_batch.build_request({"op": "edit", "key": "demo:A",
                                            "fields": {"priority": 3}},
                                           "src", "digest")
    assert verb == "edit_definition"
    assert body == {"key": "demo:A", "fields": {"priority": 3}}


def test_non_router_priority_edit_on_missing_key_refuses_like_set_priority(w):
    with pytest.raises(Refused) as via_verb:
        w.store.set_priority(w.p("l1main"), "demo:NOPE", 3)
    with pytest.raises(Refused) as via_edit:
        w.store.edit_definition(w.p("l1main"), "demo:NOPE", {"priority": 3})
    assert (via_edit.value.code, via_edit.value.msg) == \
        (via_verb.value.code, via_verb.value.msg)
    assert via_edit.value.code == "forbidden"


def test_router_priority_edit_on_missing_key_matches_set_priority(w):
    with pytest.raises(Refused) as via_verb:
        w.store.set_priority(w.p("router"), "demo:NOPE", 3)
    with pytest.raises(Refused) as via_edit:
        w.store.edit_definition(w.p("router"), "demo:NOPE", {"priority": 3})
    assert (via_edit.value.code, via_edit.value.msg) == \
        (via_verb.value.code, via_verb.value.msg)


def test_set_priority_verb_rejects_bool(w):
    with pytest.raises(Refused) as exc:
        w.store.set_priority(w.p("router"), "demo:A", True)
    assert exc.value.code == "validation"
    assert w.store._item("demo:A")["priority"] == 0
