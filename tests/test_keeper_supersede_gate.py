"""ITEM-SUPERSEDE-GATE: a superseded or duplicate item is never handed out.

``supersedes`` links retire the old item at claim and in the feeder's ready
groups; a define (every path through ``_create``) that is a near copy of an open
item is refused with the matching keys until each one is answered.
"""
from __future__ import annotations

import sqlite3

import pytest

from keeper_util import World, defn
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter


@pytest.fixture
def decisions(tmp_path):
    path = tmp_path / "decisions-log.md"
    path.write_text("| ID | project | decision |\n|---|---|---|\n", encoding="utf-8")
    return path


@pytest.fixture
def w(tmp_path, decisions):
    world = World(tmp_path)
    world.store.ctx = {"decisions": DecisionsAdapter(decisions)}
    try:
        yield world
    finally:
        world.close()


G = "demo:GOAL"
LONG = ("Reduce the feeder ready latency by caching the decision book "
        "between repeated reads of the keeper snapshot")
NEAR = ("Reduce the feeder ready latency by caching the decision book "
        "between repeated reads of the keeper snapshot today")


def _groups(w):
    return w.store._ready_groups(project="demo", n=None)


def test_supersedes_makes_the_old_item_unclaimable_and_drops_it_from_ready(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("GOAL", ["OLD"], kind="goal")])
    w.store.create_items(router, [defn("NEW", supersedes=["demo:OLD"], under=G)])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:OLD")
    assert "superseded by demo:NEW" in exc.value.msg
    groups = _groups(w)
    assert "demo:OLD" not in [r["key"] for r in groups["items"]]
    assert "demo:OLD" in [r["key"] for r in groups["needs_reconcile"]]
    assert groups["needs_reconcile"][0]["context"]["superseded_by"] == ["demo:NEW"]
    # the new item stays claimable
    assert w.store.claim(w.p("l1main"), "demo:NEW")["epoch"] == 1


def test_cancelling_the_superseder_frees_the_old_item(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("GOAL", ["OLD"], kind="goal")])
    w.store.create_items(router, [defn("NEW", supersedes=["demo:OLD"], under=G)])
    w.store.cancel(router, "demo:NEW", "wrong link")
    assert w.store.claim(w.p("l1main"), "demo:OLD")["epoch"] == 1


def test_supersedes_is_editable_and_cleared_by_an_empty_list(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("NEW"),
                                  defn("GOAL", ["OLD", "NEW"], kind="goal")])
    w.store.edit_definition(router, "demo:NEW", {"supersedes": ["OLD"]})
    assert w.store.show(router, "demo:NEW")["supersedes"] == ["demo:OLD"]
    with pytest.raises(Refused):
        w.store.claim(w.p("l1main"), "demo:OLD")
    w.store.edit_definition(router, "demo:NEW", {"supersedes": []})
    assert w.store.claim(w.p("l1main"), "demo:OLD")["epoch"] == 1


def test_supersedes_must_name_an_existing_item_and_not_itself(w):
    router = w.p("router")
    w.store.create_items(router, [defn("BASE"), defn("GOAL", ["BASE"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("NEW", supersedes=["demo:NOPE"], under=G)])
    assert "demo:NOPE" in exc.value.msg
    w.store.create_items(router, [defn("SELF", under=G)])
    with pytest.raises(Refused):
        w.store.edit_definition(router, "demo:SELF", {"supersedes": ["demo:SELF"]})


def test_retired_premise_is_refused_at_claim_and_dropped_from_ready(w, decisions):
    router = w.p("router")
    w.store.create_items(router, [defn("A", description="per D-5"),
                                  defn("GOAL", ["A"], kind="goal")])
    with decisions.open("a", encoding="utf-8") as handle:
        handle.write("| D-9 | demo | supersedes D-5 for demo |\n")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    groups = _groups(w)
    assert [r["key"] for r in groups["needs_reconcile"]] == ["demo:A"]
    assert groups["items"] == []
    # force_stale stays the audited override
    assert w.store.claim(w.p("l1main"), "demo:A", force_stale="read D-9")["epoch"] == 1


def test_define_of_a_near_copy_is_refused_with_the_match(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("COPY", title=NEAR, description=NEAR, under=G)])
    assert "demo:ORIG" in exc.value.msg
    assert "supersedes, depends_on or distinct" in exc.value.msg
    assert w.store.show(router, "demo:ORIG")  # nothing was created
    with pytest.raises(Refused):
        w.store.show(router, "demo:COPY")


@pytest.mark.parametrize("answer", ["distinct", "supersedes", "depends_on"])
def test_each_answer_lets_the_define_through_and_is_recorded(w, answer):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    w.store.create_items(router, [defn("COPY", title=NEAR, description=NEAR, under=G,
                                       similar={"demo:ORIG": answer})])
    copy = w.store.show(router, "demo:COPY")
    if answer == "supersedes":
        assert copy["supersedes"] == ["demo:ORIG"]
    if answer == "depends_on":
        assert "demo:ORIG" in [d["key"] for d in copy["depends_on"]]
    events = [e for e in keeper_events.read_events(w.conn)
              if e["key"] == "demo:COPY" and e["type"] == "item_created"]
    assert events[0]["payload"]["similar"] == {"demo:ORIG": answer}


def test_an_unanswered_match_stays_refused_when_another_is_answered(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("ORIG2", title=LONG, description=LONG + " twice"),
                                  defn("GOAL", ["ORIG", "ORIG2"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("COPY", title=NEAR, description=NEAR, under=G,
                                           similar={"demo:ORIG": "distinct"})])
    assert "demo:ORIG2" in exc.value.msg and "demo:ORIG:" not in exc.value.msg


def test_an_answer_outside_the_three_is_refused(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("COPY", title=NEAR, description=NEAR, under=G,
                                           similar={"demo:ORIG": "maybe"})])
    assert "supersedes, depends_on or distinct" in exc.value.msg


def test_apply_ops_create_of_a_near_copy_is_refused_too(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(router, [{"op": "create",
                                    "item": defn("COPY", title=NEAR, description=NEAR, under=G)}])
    assert "demo:ORIG" in str(exc.value)


def test_short_or_unrelated_items_are_never_matches(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    w.store.create_items(router, [defn("T1", under=G), defn("T2", title="Rotate the web token cache", under=G)])
    # a cancelled item is terminal: no match
    w.store.cancel(router, "demo:T1", "x")
    w.store.cancel(router, "demo:T2", "x")
    w.store.create_items(router, [defn("T3", title="title T1", under=G)])


def test_a_database_without_the_new_column_upgrades_and_loads(tmp_path):
    from plangraph.keeper import db as kdb
    world = World(tmp_path, backend="sqlite")
    world.store.create_items(world.p("router"), [defn("A"), defn("GOAL", ["A"], kind="goal")])
    world.conn.close()
    raw = sqlite3.connect(world.path)
    raw.execute("ALTER TABLE items DROP COLUMN supersedes")
    raw.commit()
    raw.close()
    conn = kdb.open_db(world.path, backend="sqlite")
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        assert "supersedes" in columns
        assert conn.execute("SELECT supersedes FROM items WHERE key='demo:A'").fetchone()[0] is None
    finally:
        conn.close()


def test_boilerplate_descriptions_and_short_titles_never_match(w):
    router = w.p("router")
    text = "Why: test bounds\nHow: validate priority and range of the stored value"
    w.store.create_items(router, [defn("A", title="A", description=text),
                                  defn("B", title="Gate", description=text),
                                  defn("GOAL", ["A", "B"], kind="goal")])
    # same boilerplate, different long titles: the title decides
    w.store.create_items(router, [
        defn("C", title="Rotate the web token cache nightly", description=text, under=G),
        defn("D", title="Split the oversize review prompt builder", description=text, under=G)])
