"""Role rules, reviewer guards, evidence rules and graph validation."""
import pytest

from keeper_util import (FakeVerifier, World, defn, seed, to_claimed, to_done,
                         to_in_review)
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


# ---------------------------------------------------------------- create/role

def test_router_can_create_any_project(w):
    seed(w, [("A", [])], project="other")
    assert w.store._item("other:A")["state"] == "open"


def test_l1_can_create_own_project(w):
    seed(w, [("A", [])], project="demo")
    assert w.store._item("demo:A")["state"] == "open"


def test_l1_cannot_create_other_project(w):
    items = [defn("A", project="other"),
             defn("GOAL", project="other", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("l1main"), items)
    assert exc.value.code == "forbidden"


def test_worker_cannot_create(w):
    items = [defn("A"), defn("GOAL", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("workerA"), items)
    assert exc.value.code == "forbidden"


def test_reader_cannot_claim(w):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("reader"), key)
    assert exc.value.code == "forbidden"


def test_l1_cannot_cancel_other_project(w):
    (key,) = seed(w, [("A", [])], project="other")
    # Retire the goal first: the owning L1's success must prove project scope,
    # not trip the separate no-active-dependents rule.
    w.store.cancel(w.p("router"), "other:GOAL", "drop goal")
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p("l1main"), key, "nope")
    assert exc.value.code == "forbidden"
    w.store.cancel(w.p("l1other"), key, "mine to drop")


# ------------------------------------------------------------- graph rules

def test_create_rejects_cycle(w):
    items = [defn("A", deps=["B"]), defn("B", deps=["A"]),
             defn("GOAL", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "cycle"


def test_create_rejects_unknown_dep(w):
    items = [defn("A", deps=["GHOST"]),
             defn("GOAL", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "validation"


def test_create_rejects_unreachable_goal(w):
    items = [defn("A"), defn("B"),
             defn("GOAL", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "validation"


def test_corrects_non_done_refused(w):
    seed(w, [("A", [])])
    items = [defn("FIX", deps=["A"], corrects="demo:A"),
             defn("GOAL2", kind="goal", deps=["A", "FIX"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "validation"


def test_corrects_done_ok_and_recorded(w):
    (key,) = seed(w, [("A", [])])
    to_done(w, key)
    items = [defn("FIX", deps=[], corrects=key), defn("GOAL2", kind="goal", deps=["FIX"])]
    w.store.create_items(w.p("router"), items)
    snap = w.store.snapshot(w.p("reader"))
    fixed = {it["key"]: it for it in snap["items"]}["demo:A"]
    assert fixed["corrected_by"] == ["demo:FIX"]


def test_edit_definition_open_only(w):
    (key,) = seed(w, [("A", [])])
    w.store.edit_definition(w.p("router"), key, {"title": "renamed"})
    assert w.store._item(key)["title"] == "renamed"
    to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), key, {"title": "again"})
    assert exc.value.code == "bad_state"


# ------------------------------------------------------------------ evidence

def test_submit_requires_evidence(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    epoch = to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), key, epoch)
    assert exc.value.code == "evidence_missing"


def test_submit_refuses_unverified_evidence(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    w.verifiers["decision"].ok = False
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), key, epoch)
    assert exc.value.code == "evidence_unverified"


def test_unknown_evidence_kind_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.add_evidence(w.p("l1main"), key, epoch, "bogus", {})
    assert exc.value.code == "validation"


def test_add_evidence_by_stranger_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.add_evidence(w.p("workerB"), key, epoch, "decision", {"id": "D-1"})
    assert exc.value.code == "forbidden"


def test_verdict_reverifies_evidence(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    w.verifiers["decision"].ok = False
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "looks fine")
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(key)["state"] == "in_review"


def test_router_batch_evidence_attaches(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    to_in_review(w, key)
    w.store.add_evidence_router(w.p("router"), key, "decision", {"id": "D-9"})
    rows = w.conn.execute("SELECT verified FROM evidence WHERE key=?", (key,)).fetchall()
    assert rows and rows[0][0] == 1


# ------------------------------------------------------------------ reviewer

def test_reviewer_cannot_be_claimant(w):
    # A legacy in-review row claimed by the principal who would review it.
    items = [{"project": "demo", "id": "A", "title": "a", "kind": "lane",
              "state": "in_review", "owner": "reviewer", "depends_on": [],
              "evidence_kinds": ["any"]},
             {"project": "demo", "id": "GOAL", "title": "g", "kind": "goal",
              "state": "open", "depends_on": ["demo:A"], "evidence_kinds": ["any"]}]
    w.store.import_items(w.p("router"), items)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "demo:A", True, "self")
    assert exc.value.code == "same_principal"


def test_reviewer_same_family_refused(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key, who="l1main")  # family teamA
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewerSame"), key, True, "cozy")
    assert exc.value.code == "same_family"


def test_reviewer_unknown_family_refused(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key, who="l1main")
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewerNoFam"), key, True, "who?")
    assert exc.value.code == "family_unknown"


def test_verdict_requires_reviewer_role(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("workerA"), key, True, "not mine")
    assert exc.value.code == "forbidden"


# ------------------------------------------------------------- delegate/forgery

def test_delegate_must_be_worker(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.delegate(w.p("l1main"), key, "reader")
    assert exc.value.code == "validation"
    w.store.delegate(w.p("l1main"), key, "workerA")
    assert w.store._item(key)["delegate"] == "workerA"


def test_delegate_only_by_claimant(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key, who="workerA")
    with pytest.raises(Refused) as exc:
        w.store.delegate(w.p("l1main"), key, "workerB")
    assert exc.value.code == "forbidden"


def test_forged_actor_field_is_ignored(w):
    items = [defn("A", actor="router", actor_role="router"),
             defn("GOAL", kind="goal", deps=["A"])]
    w.store.create_items(w.p("l1main"), items)
    actor = w.conn.execute(
        "SELECT actor FROM events WHERE key='demo:A'").fetchone()[0]
    assert actor == "l1main"
