"""ITEM-SUPERSEDE-GATE: a superseded or duplicate item is never handed out.

``supersedes`` links retire the old item at claim and in the feeder's ready
groups; a define (every path through ``_create``) that is a near copy of an open
item is refused with the matching keys until each one is answered.
"""
from __future__ import annotations

import sqlite3

import pytest

from keeper_util import World, defn, to_done
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
OTHER_LONG = ("Trim repeated sections from every review prompt card "
              "before the reviewer seat reads the diff")
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
                                  defn("ORIG2", title=LONG, description=LONG + " twice",
                                       similar={"demo:ORIG": "distinct"}),
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
    w.store.create_items(router, [defn("T1", title=OTHER_LONG, description=OTHER_LONG, under=G),
                                  defn("T2", title="Rotate the web token cache", under=G)])
    # a cancelled item is terminal: its near copy is no match
    w.store.cancel(router, "demo:T1", "x")
    w.store.cancel(router, "demo:T2", "x")
    w.store.create_items(router, [defn("T3", title=OTHER_LONG, description=OTHER_LONG, under=G)])


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


def test_a_done_superseder_keeps_the_old_item_retired(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("GOAL", ["OLD"], kind="goal")])
    w.store.create_items(router, [defn("NEW", supersedes=["demo:OLD"], under=G)])
    to_done(w, "demo:NEW")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:OLD")
    assert "superseded by demo:NEW" in exc.value.msg
    assert "demo:OLD" not in [r["key"] for r in _groups(w)["items"]]


def test_a_near_copy_of_a_different_kind_is_still_a_match(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", kind="goal", title=LONG, description=LONG,
                                       done_criteria=None)])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("COPY", title=NEAR, description=NEAR)])
    assert "demo:ORIG" in exc.value.msg


def test_define_edit_of_a_title_into_a_near_copy_is_refused_until_answered(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("OTHER", title="Unrelated work on the web page cache layer"),
                                  defn("GOAL", ["ORIG", "OTHER"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(router, "demo:OTHER", {"title": NEAR, "description": NEAR})
    assert "demo:ORIG" in exc.value.msg and "supersedes, depends_on or distinct" in exc.value.msg
    assert w.store.show(router, "demo:OTHER")["title"].startswith("Unrelated")   # nothing written
    with pytest.raises(Refused):                                                   # apply_ops edit too
        w.store.apply_ops(router, [{"op": "edit", "key": "demo:OTHER",
                                    "fields": {"title": NEAR, "description": NEAR}}])
    w.store.edit_definition(router, "demo:OTHER", {"title": NEAR, "description": NEAR,
                                                   "similar": {"demo:ORIG": "supersedes"}})
    assert w.store.show(router, "demo:OTHER")["supersedes"] == ["demo:ORIG"]
    created = [e for e in keeper_events.read_events(w.conn)
               if e["key"] == "demo:OTHER" and e["type"] == "item_defined"
               and e["payload"].get("similar")]
    assert created and created[-1]["payload"]["similar"] == {"demo:ORIG": "supersedes"}


def test_an_edit_without_answers_leaves_the_event_payload_unchanged(w):
    router = w.p("router")
    w.store.create_items(router, [defn("A", title="Plain item about the cache layer"),
                                  defn("GOAL", ["A"], kind="goal")])
    w.store.edit_definition(router, "demo:A", {"title": "Plain item about the cache layer v2"})
    last = [e for e in keeper_events.read_events(w.conn)
            if e["key"] == "demo:A" and e["type"] == "item_defined"][-1]
    assert last["payload"]["fields"] == ["title"] and "similar" not in last["payload"]


def test_define_edit_unrelated_fields_and_distinct_answers_pass(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("OTHER", title="Unrelated work on the web page cache layer"),
                                  defn("GOAL", ["ORIG", "OTHER"], kind="goal")])
    w.store.edit_definition(router, "demo:OTHER", {"owner": "someone"})          # no text change
    w.store.edit_definition(router, "demo:OTHER", {"title": NEAR, "description": NEAR,
                                                   "similar": {"demo:ORIG": "distinct"}})


def test_a_similar_mapping_with_a_non_string_key_is_a_validation_refusal(w):
    router = w.p("router")
    w.store.create_items(router, [defn("BASE"), defn("GOAL", ["BASE"], kind="goal")])
    for bad in ({1: "distinct"}, {"": "distinct"}, ["demo:A"]):
        with pytest.raises(Refused) as exc:
            w.store.create_items(router, [defn("X", similar=bad, under=G)])
        assert exc.value.code == "validation" and "similar" in exc.value.msg


def test_a_database_without_the_category_column_upgrades_and_loads(tmp_path):
    from plangraph.keeper import db as kdb
    world = World(tmp_path, backend="sqlite")
    world.store.create_items(world.p("router"), [defn("A"), defn("GOAL", ["A"], kind="goal")])
    world.conn.close()
    raw = sqlite3.connect(world.path)
    raw.execute("ALTER TABLE items DROP COLUMN category")
    raw.execute("DROP TABLE categories")
    raw.commit()
    raw.close()
    conn = kdb.open_db(world.path, backend="sqlite")
    try:
        assert "category" in [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        assert [r[0] for r in conn.execute(
            "SELECT name FROM categories WHERE project='*' ORDER BY rank")][:2] == ["speed", "efficiency-tokens"]
        assert conn.execute("SELECT category FROM items WHERE key='demo:A'").fetchone()[0] is None
    finally:
        conn.close()


# ------------------------------------------------------------ review round 2
def test_system_and_worker_claims_refuse_a_superseded_item_too(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("GOAL", ["OLD"], kind="goal")])
    w.store.create_items(router, [defn("NEW", supersedes=["demo:OLD"], under=G)])
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:OLD")      # the system claim skips the stale checks
    assert "superseded by demo:NEW" in exc.value.msg
    assert w.store.show(router, "demo:OLD")["state"] == "open"


def test_a_link_added_after_the_prepare_step_still_refuses_the_claim(w, monkeypatch):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("NEW"),
                                  defn("GOAL", ["OLD", "NEW"], kind="goal")])
    real = w.store._prepare_claim

    def racing(*args, **kwargs):
        prepared = real(*args, **kwargs)
        w.store.edit_definition(router, "demo:NEW", {"supersedes": ["demo:OLD"]})   # lands after the check
        return prepared
    monkeypatch.setattr(w.store, "_prepare_claim", racing)
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:OLD")
    assert "superseded by demo:NEW" in exc.value.msg


def test_similar_answers_cannot_link_across_projects_without_the_router(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", project="other", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], project="other", kind="goal")])
    w.store.create_items(router, [defn("MINE"), defn("DGOAL", ["MINE"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), "demo:MINE", {"supersedes": ["other:ORIG"]})
    assert exc.value.code == "forbidden"
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("l1main"), [defn("COPY", title=LONG, description=LONG, under="demo:DGOAL",
                                                  similar={"other:ORIG": "supersedes"})])
    assert exc.value.code == "forbidden"
    w.store.edit_definition(router, "demo:MINE", {"supersedes": ["other:ORIG"]})   # the router may


def test_a_batch_cannot_hold_two_near_copies(w):
    router = w.p("router")
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("ONE", title=LONG, description=LONG),
                                      defn("TWO", title=LONG, description=LONG),
                                      defn("GOAL", ["ONE", "TWO"], kind="goal")])
    assert "demo:ONE" in exc.value.msg and "demo:TWO" in exc.value.msg
    w.store.create_items(router, [defn("ONE", title=LONG, description=LONG),
                                  defn("TWO", title=LONG, description=LONG, similar={"demo:ONE": "distinct"}),
                                  defn("GOAL", ["ONE", "TWO"], kind="goal")])
    # slices of one parent are the same job split on purpose
    w.store.create_items(router, [defn("S1", title=OTHER_LONG + " part one", slice_parent="demo:ONE", under=G),
                                  defn("S2", title=OTHER_LONG + " part one", slice_parent="demo:ONE", under=G)])


# ------------------------------------------------------------ review round 3
def test_a_granted_l1_batch_cannot_link_or_answer_outside_its_projects(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", project="other", title=LONG, description=LONG),
                                  defn("OGOAL", ["ORIG"], project="other", kind="goal"),
                                  defn("MINE"), defn("DGOAL", ["MINE"], kind="goal")])
    w.store.grant_apply(router, "l1main", "demo")
    for fields in ({"supersedes": ["other:ORIG"]},):
        with pytest.raises(Refused) as exc:
            w.store.apply_ops(w.p("l1main"), [{"op": "edit", "key": "demo:MINE", "fields": fields}])
        assert exc.value.code == "forbidden" and "other:ORIG" in exc.value.msg
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [{"op": "create", "item": defn(
            "COPY", title=LONG, description=LONG, under="demo:DGOAL", similar={"other:ORIG": "supersedes"})}])
    assert exc.value.code == "forbidden"
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [{"op": "create", "item": defn(
            "COPY2", title=LONG, description=LONG, under="demo:DGOAL", supersedes=["other:ORIG"])}])
    assert exc.value.code == "forbidden"


def test_a_three_item_supersession_cycle_is_refused(w):
    router = w.p("router")
    w.store.create_items(router, [defn("A"), defn("B"), defn("C"),
                                  defn("GOAL", ["A", "B", "C"], kind="goal")])
    w.store.edit_definition(router, "demo:A", {"supersedes": ["demo:B"]})
    w.store.edit_definition(router, "demo:B", {"supersedes": ["demo:C"]})
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(router, "demo:C", {"supersedes": ["demo:A"]})
    assert "cycle" in exc.value.msg


def test_rehoming_a_superseded_item_keeps_it_retired_under_its_new_key(w):
    router = w.p("router")
    w.store.create_items(router, [defn("OLD"), defn("GOAL", ["OLD"], kind="goal")])
    w.store.create_items(router, [defn("NEW", supersedes=["demo:OLD"], under=G)])
    new_key = w.store.rehome(router, "demo:OLD", "other", None)["new"]
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1other"), new_key)
    assert "superseded by demo:NEW" in exc.value.msg
    assert new_key not in [r["key"] for r in w.store._ready_groups(project="other", n=None)["items"]]
    snap = {i["key"]: i for i in w.store._snapshot(w.clock())["items"]}
    assert snap[new_key]["superseded_by"] == ["demo:NEW"]


def test_exact_duplicates_with_a_short_title_are_matches(w):
    router = w.p("router")
    w.store.create_items(router, [defn("LOGIN", title="Fix login", description=LONG, note="files: src/a.py"),
                                  defn("GOAL", ["LOGIN"], kind="goal")])
    with pytest.raises(Refused) as exc:
        w.store.create_items(router, [defn("LOGIN2", title="Fix login", description=LONG,
                                           note="files: src/a.py", under=G)])
    assert "demo:LOGIN" in exc.value.msg
    w.store.create_items(router, [defn("OTHERFIX", title="Fix login", description=OTHER_LONG, under=G)])


def test_two_identical_siblings_in_one_import_need_an_answer(w):
    router = w.p("router")
    first, second = (defn(n, title=LONG, description=LONG) for n in ("S1", "S2"))
    goal = defn("GOAL", ["S1", "S2"], kind="goal")
    with pytest.raises(Refused) as exc:
        w.store.import_items(router, [first, second, goal])
    assert "demo:S1" in exc.value.msg
    w.store.import_items(router, [first, {**second, "similar": {"demo:S1": "distinct"}}, goal])


def test_answers_of_one_type_are_each_counted_and_survive_a_refused_batch(w):
    router = w.p("router")
    w.store.create_items(router, [defn("O1", title=LONG, description=LONG),
                                  defn("O2", title=LONG, description=LONG + " twice",
                                       similar={"demo:O1": "distinct"}),
                                  defn("GOAL", ["O1", "O2"], kind="goal")])
    before = dict(w.conn.execute("SELECT metric, value FROM metrics WHERE metric LIKE 'similar_%'").fetchall())
    copy = defn("C1", title=NEAR, description=NEAR, under=G, similar={"demo:O1": "distinct", "demo:O2": "distinct"})
    bad = defn("BAD", title=LONG, description=LONG, under=G)           # unanswered: the batch is refused
    with pytest.raises(Refused):
        w.store.apply_ops(router, [{"op": "create", "item": copy}, {"op": "create", "item": bad}])
    after = dict(w.conn.execute("SELECT metric, value FROM metrics WHERE metric LIKE 'similar_%'").fetchall())
    assert after["similar_answered.distinct"] - before.get("similar_answered.distinct", 0) == 2
    assert after["similar_matches_shown"] > before.get("similar_matches_shown", 0)


# ------------------------------------------------------------ review round 4
def test_a_cycle_through_a_rehome_alias_is_refused(w):
    router = w.p("router")
    w.store.create_items(router, [defn("A"), defn("B"), defn("GOAL", ["A", "B"], kind="goal")])
    w.store.edit_definition(router, "demo:A", {"supersedes": ["demo:B"]})
    new_b = w.store.rehome(router, "demo:B", "other", None)["new"]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), new_b, {"supersedes": ["demo:A"]})
    assert "cycle" in exc.value.msg


def test_a_dry_run_does_not_count_the_experiment(w):
    router = w.p("router")
    w.store.create_items(router, [defn("ORIG", title=LONG, description=LONG),
                                  defn("GOAL", ["ORIG"], kind="goal")])
    copy = defn("COPY", title=NEAR, description=NEAR, under=G, similar={"demo:ORIG": "distinct"})
    q = "SELECT COALESCE(SUM(value),0) FROM metrics WHERE metric LIKE 'similar_%'"
    before = w.conn.execute(q).fetchone()[0]
    w.store.apply_ops(router, [{"op": "create", "item": copy}], dry_run=True)
    assert w.conn.execute(q).fetchone()[0] == before
    w.store.apply_ops(router, [{"op": "create", "item": copy}])
    assert w.conn.execute(q).fetchone()[0] > before
