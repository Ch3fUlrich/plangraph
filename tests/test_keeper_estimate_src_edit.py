"""PG-ESTIMATE-SRC-EDIT: ``edit_definition`` can set ``estimate_src``.

``estimate_src`` is the provenance marker audit-meta reads (``docs/SPEC.md``:
``default`` = the plan stated no number), so an ``estimate_h`` edit that leaves
it behind keeps flagging the item forever.  Now it is editable, an edit that
carries only ``estimate_h`` attributes the number to the editing principal, and
``estimate_src`` alone needs a non-null ``estimate_h``.

Mutations killed: ``estimate_src`` still rejected as "not editable"; an
``estimate_h`` edit that keeps ``src: default``; an ``estimate_src`` written by
an l1 of another project or onto a claimed item; a blank/oversize/non-string
source, a credential-shaped one, or one smuggled past the cap; a payload that
omits the stored estimate pair; the audit-meta flag surviving the edit; and any
write here breaking ``verify_integrity``.
"""
from __future__ import annotations

import json

import pytest

from door_util import Doors
from keeper_util import World, defn, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

SECRET = "svcacct/Zq9xk2LmPw"


@pytest.fixture
def w(tmp_path):
    """``demo:A`` (estimate, no src), ``demo:NOH`` (no estimate), ``demo:DEF``
    (the SPEC ``default`` marker) behind a goal, plus one item in ``other``."""
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"),                                         # estimate_h 1, src unset
        defn("NOH", estimate_h=None),                      # no estimate at all
        defn("DEF", estimate_h=3, estimate_src="default"),  # the default marker
        defn("GOAL", ["A", "NOH", "DEF"], kind="goal")])
    world.store.create_items(world.p("router"), [
        defn("F", project="other"),                        # l1 scope probe
        defn("OG", ["F"], project="other", kind="goal")])
    yield world
    world.close()


@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def _payloads(w, key):
    """Every ``item_defined`` payload written for *key*, oldest first."""
    rows = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='item_defined'"
        " ORDER BY seq", (key,))
    return [json.loads(row[0]) for row in rows]


def _events(w, key):
    rows = w.conn.execute(
        "SELECT type, payload FROM events WHERE key=? ORDER BY seq", (key,))
    return [(row[0], json.loads(row[1])) for row in rows]


def _estimate_keys(w, who="router", project="demo"):
    """The keys audit-meta flags for a missing ``estimate_h`` in *project*."""
    data = w.store.audit_meta(w.p(who), project=project)
    proj = next(p for p in data["projects"] if p["project"] == project)
    return next(m["keys"] for m in proj["missing"] if m["field"] == "estimate_h")


# --------------------------------------------------------------- happy paths

def test_router_sets_estimate_h_and_estimate_src_explicitly(w):
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"estimate_h": 5, "estimate_src": "plan"})
    item = w.store._item("demo:A")
    assert (item["estimate_h"], item["estimate_src"]) == (5, "plan")
    # the event says what the edit stored -- names AND values, nothing dropped
    payload = _payloads(w, "demo:A")[-1]
    assert payload["fields"] == ["estimate_h", "estimate_src"]
    assert payload["estimate_h"] == 5 and payload["estimate_src"] == "plan"
    assert "estimate_src_auto" not in payload        # an explicit value, not a fill-in
    assert verify_integrity(w.conn) == [] and w.store.verify_integrity() == []


def test_estimate_h_edit_without_src_attributes_the_editor(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"estimate_h": 4})
    assert w.store._item("demo:A")["estimate_src"] == "router"   # no longer "default"
    assert _payloads(w, "demo:A")[-1].get("estimate_src_auto") is True
    # the l1 variant attributes the number to the l1, not to the router
    w.store.edit_definition(w.p("l1main"), "demo:DEF", {"estimate_h": 2})
    assert w.store._item("demo:DEF")["estimate_src"] == "l1main"
    assert _payloads(w, "demo:DEF")[-1] == {
        "fields": ["estimate_h", "estimate_src"], "estimate_h": 2,
        "estimate_src": "l1main", "estimate_src_auto": True}
    assert verify_integrity(w.conn) == []


def test_estimate_src_alone_is_allowed_when_the_item_has_an_estimate(w):
    w.store.edit_definition(w.p("l1main"), "demo:A", {"estimate_src": "plan"})
    assert w.store._item("demo:A")["estimate_src"] == "plan"
    assert w.store._item("demo:A")["estimate_h"] == 1        # untouched
    assert _payloads(w, "demo:A")[-1] == {"fields": ["estimate_src"],
                                          "estimate_src": "plan"}
    assert verify_integrity(w.conn) == []


# ----------------------------------------------------------------- audit-meta

def test_audit_meta_flags_a_default_item_and_stops_after_the_edit(w):
    assert "demo:DEF" in _estimate_keys(w)               # estimate_src: default
    assert "demo:NOH" in _estimate_keys(w)               # estimate_h NULL
    w.store.edit_definition(w.p("router"), "demo:DEF", {"estimate_src": "plan"})
    w.store.edit_definition(w.p("router"), "demo:NOH", {"estimate_h": 2})
    after = _estimate_keys(w)
    assert "demo:DEF" not in after and "demo:NOH" not in after
    assert "demo:A" not in after                          # a plain estimate was fine
    assert verify_integrity(w.conn) == []


def test_the_audit_read_still_sees_the_explicit_source(w):
    w.store.edit_definition(w.p("router"), "demo:A", {"estimate_src": "default"})
    assert "demo:A" in _estimate_keys(w)                  # back to the marker
    assert verify_integrity(w.conn) == []


# ------------------------------------------------------------------- refusals

def test_estimate_src_without_an_estimate_h_is_refused(w):
    before = _events(w, "demo:NOH")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:NOH", {"estimate_src": "plan"})
    assert (exc.value.code, exc.value.msg) == \
        ("validation", "estimate_src needs an estimate_h")
    assert _events(w, "demo:NOH") == before               # a refusal writes nothing
    assert w.store._item("demo:NOH")["estimate_src"] is None
    assert verify_integrity(w.conn) == []


def test_clearing_estimate_h_clears_the_source_and_is_not_auto_filled(w):
    router = w.p("router")
    w.store.edit_definition(router, "demo:DEF", {"estimate_h": None})
    row = w.conn.execute(
        "SELECT estimate_h, estimate_src FROM items WHERE key='demo:DEF'").fetchone()
    assert row["estimate_h"] is None and row["estimate_src"] is None
    assert "estimate_src_auto" not in _payloads(w, "demo:DEF")[-1]
    assert verify_integrity(w.conn) == []


def test_a_source_cannot_be_set_while_the_estimate_is_being_cleared(w):
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A",
                                {"estimate_h": None, "estimate_src": "plan"})
    assert exc.value.code == "validation"
    row = w.conn.execute(
        "SELECT estimate_h FROM items WHERE key='demo:A'").fetchone()
    assert row["estimate_h"] is not None          # nothing was written


@pytest.mark.parametrize("bad", ["", "   ", None, 5, True, ["plan"], "x" * 65])
def test_blank_non_string_and_oversize_sources_are_refused(w, bad):
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"estimate_src": bad})
    assert exc.value.code == "validation"
    assert "estimate_src" in exc.value.msg
    assert _events(w, "demo:A") == before
    assert verify_integrity(w.conn) == []


def test_the_64_character_cap_boundary_still_fits(w):
    value = "x" * 64
    w.store.edit_definition(w.p("router"), "demo:A", {"estimate_src": value})
    assert w.store._item("demo:A")["estimate_src"] == value
    assert verify_integrity(w.conn) == []


def test_credential_shaped_source_is_refused_and_never_echoed(w):
    before = _events(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A",
                                {"estimate_src": f"login {SECRET}"})
    assert exc.value.code == "validation"
    assert "estimate_src" in exc.value.msg
    assert "Zq9xk2" not in str(exc.value)
    assert _events(w, "demo:A") == before
    assert verify_integrity(w.conn) == []


def test_an_estimate_src_field_still_needs_the_router_or_owning_l1(w):
    before = _events(w, "other:F")
    with pytest.raises(Refused) as exc:                    # l1 of demo, item in other
        w.store.edit_definition(w.p("l1main"), "other:F", {"estimate_src": "plan"})
    assert exc.value.code == "forbidden"
    with pytest.raises(Refused) as exc:                    # a role that may not edit
        w.store.edit_definition(w.p("workerA"), "demo:A", {"estimate_src": "plan"})
    assert exc.value.code == "forbidden"
    assert _events(w, "other:F") == before
    assert verify_integrity(w.conn) == []


def test_a_claimed_item_is_still_bad_state(w):
    to_claimed(w, "demo:A", who="l1main")
    before = _events(w, "demo:A")
    for fields in ({"estimate_src": "plan"}, {"estimate_h": 4},
                   {"estimate_h": 4, "estimate_src": "plan"}):
        with pytest.raises(Refused) as exc:
            w.store.edit_definition(w.p("router"), "demo:A", fields)
        assert exc.value.code == "bad_state"
    assert _events(w, "demo:A") == before
    assert verify_integrity(w.conn) == []


# --------------------------------------------------------------------- doors

def test_the_http_door_passes_estimate_src_through_without_a_door_change(w, doors):
    status, data = doors.http("l1main", "POST", "/v1/edit_definition",
                              {"key": "demo:A", "fields": {"estimate_h": 6}})
    assert (status, data) == (200, {"key": "demo:A"})
    assert w.store._item("demo:A")["estimate_src"] == "l1main"
    status, data = doors.http("router", "POST", "/v1/edit_definition",
                              {"key": "demo:DEF", "fields": {"estimate_src": "plan"}})
    assert (status, data) == (200, {"key": "demo:DEF"})
    assert w.store._item("demo:DEF")["estimate_src"] == "plan"
    status, body = doors.http("router", "POST", "/v1/edit_definition",
                              {"key": "demo:NOH", "fields": {"estimate_src": "plan"}})
    assert status == 400 and body["error"] == "validation"
    assert verify_integrity(w.conn) == []
