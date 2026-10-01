"""Hardening tests for findings 1-4 of the adversarial keeper-store review.

Finding 1: UPDATE triggers must check NEW.key as well as OLD.key.
Finding 2: ``_reverify_all`` must only re-verify verified rows of the current epoch.
Finding 3: ``verdict(ok)`` must run the same evidence-kind check as submit; a
           legacy item carries an ``owner_family`` so reviewer independence works.
Finding 4: project scope applies to workers and reviewers too.
"""
import sqlite3

import pytest

from keeper_util import World, defn, seed, to_claimed, to_done
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_types import Result

# Store-layer findings (verdict evidence requirement, project scope, the
# `_reverify_all` epoch filter and the reviewer-family rule).  These stay as
# they are; the next writer removes the marks once the store is fixed.
STORE_LAYER = "store layer, next task"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


# --------------------------------------------------------------- helpers

def legacy(pid, *, state="in_review", owner="legacy@example.invalid",
           owner_family=None, evidence_kinds=("any",), project="demo",
           kind="lane", deps=()):
    item = {"project": project, "id": pid, "title": pid, "kind": kind,
            "state": state, "owner": owner, "depends_on": list(deps),
            "evidence_kinds": list(evidence_kinds)}
    if owner_family is not None:
        item["owner_family"] = owner_family
    return item


def legacy_goal(deps, project="demo"):
    return legacy("GOAL", state="open", owner=None, project=project, kind="goal",
                  deps=[f"{project}:{d}" for d in deps])


def ref_verifier(bad):
    class _V:
        def verify(self, item, ref, ctx):
            ok = (ref or {}).get("id") not in bad
            return Result(ok, "ok" if ok else "stale", dict(ref or {}))
    return _V()


# =============================================================== finding 1

def _done_and_other(w):
    keys = seed(w, [("P", []), ("X", ["P"])])
    to_done(w, keys[0])
    epoch = to_claimed(w, keys[1])
    w.store.add_evidence(w.p("l1main"), keys[1], epoch, "decision", {"id": "D-1"})
    return keys


def test_evidence_key_move_onto_done_aborts(w):
    done, other = _done_and_other(w)
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE evidence SET key=? WHERE key=?", (done, other))


def test_evidence_key_move_from_done_aborts(w):
    done, other = _done_and_other(w)
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE evidence SET key=? WHERE key=?", (other, done))


def test_edge_src_move_onto_done_aborts(w):
    done, other = _done_and_other(w)
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE edges SET src=? WHERE src=?", (done, other))


def test_item_key_move_onto_event_locked_key_aborts(w):
    _, other = _done_and_other(w)
    # A review_passed event locks a key even when no items row exists for it,
    # so the move does not hit the primary-key check: only NEW.key can stop it.
    w.store.conn.execute(
        "INSERT INTO events(ts,actor,role,model,key,type,payload,prev_hash,hash)"
        " VALUES('t','router','router',NULL,'demo:PHANTOM','review_passed',"
        "'{}','','h')")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE items SET key='demo:PHANTOM' WHERE key=?", (other,))


# =============================================================== finding 2

def test_verdict_ignores_stored_unverified_rows(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    w.store.verifiers["decision"] = ref_verifier({"D-BAD"})
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-BAD"})
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-GOOD"})
    w.store.submit(w.p("l1main"), key, epoch)
    w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert w.store._item(key)["state"] == "done"


def test_verdict_ignores_evidence_of_other_epochs(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    verifier = ref_verifier(set())
    w.store.verifiers["decision"] = verifier
    epoch1 = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch1, "decision", {"id": "D-BAD"})
    w.store.release(w.p("l1main"), key, epoch1)
    epoch2 = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch2, "decision", {"id": "D-GOOD"})
    w.store.verifiers["decision"] = ref_verifier({"D-BAD"})
    w.store.submit(w.p("l1main"), key, epoch2)
    w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert w.store._item(key)["state"] == "done"


# =============================================================== finding 3

def test_verdict_requires_every_evidence_kind(w):
    items = [legacy("A", evidence_kinds=("commit+ci",), owner_family="teamZ"),
             legacy_goal(["A"])]
    w.store.import_items(w.p("router"), items)
    w.store.add_evidence_router(w.p("router"), "demo:A", "decision", {"id": "D-1"})
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "demo:A", True, "ok")
    assert exc.value.code == "evidence_missing"
    assert w.store._item("demo:A")["state"] == "in_review"


def test_legacy_owner_family_blocks_same_family_review(w):
    items = [legacy("A", owner_family="teamC"), legacy_goal(["A"])]
    w.store.import_items(w.p("router"), items)
    w.store.add_evidence_router(w.p("router"), "demo:A", "decision", {"id": "D-1"})
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "demo:A", True, "ok")
    assert exc.value.code == "same_family"


def test_legacy_without_owner_family_is_family_unknown(w):
    items = [legacy("A"), legacy_goal(["A"])]
    w.store.import_items(w.p("router"), items)
    w.store.add_evidence_router(w.p("router"), "demo:A", "decision", {"id": "D-1"})
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "demo:A", True, "ok")
    assert exc.value.code == "family_unknown"


def test_legacy_with_owner_family_can_be_reviewed(w):
    items = [legacy("A", owner_family="teamZ"), legacy_goal(["A"])]
    w.store.import_items(w.p("router"), items)
    w.store.add_evidence_router(w.p("router"), "demo:A", "decision", {"id": "D-1"})
    w.store.verdict(w.p("reviewer"), "demo:A", True, "ok")
    assert w.store._item("demo:A")["state"] == "done"


# =============================================================== finding 4

def test_worker_cannot_claim_other_project(w):
    seed(w, [("A", [])], project="other")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("workerA"), "other:A")
    assert exc.value.code == "forbidden"


def test_reviewer_cannot_review_other_project(w):
    seed(w, [("A", [])], project="other")
    epoch = to_claimed(w, "other:A", who="l1other")
    w.store.add_evidence(w.p("l1other"), "other:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1other"), "other:A", epoch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "other:A", True, "x")
    assert exc.value.code == "forbidden"


def test_delegate_target_must_be_scoped_to_project(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key)
    token = auth.add_principal(w.conn, "workerOther", "worker", ["other"],
                               family="teamD")
    worker = auth.authenticate(w.conn, token)
    with pytest.raises(Refused) as exc:
        w.store.delegate(w.p("l1main"), key, worker)
    assert exc.value.code == "forbidden"
    assert w.store._item(key)["delegate"] is None


def test_empty_projects_means_no_project(w):
    token = auth.add_principal(w.conn, "reviewerNone", "reviewer", [], family="teamQ")
    reviewer = auth.authenticate(w.conn, token)
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), key, epoch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(reviewer, key, True, "x")
    assert exc.value.code == "forbidden"
