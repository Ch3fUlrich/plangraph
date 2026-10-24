"""Hardening tests for findings 5-7 of the adversarial keeper-store review.

Finding 5: reads/validation inside BEGIN IMMEDIATE + guarded UPDATEs.
Finding 6: l1 cannot cancel an item with active dependents; validation only
           refuses problems that involve the new/changed keys.
Finding 7: fold/integrity agree for delegate, lease_until, reviewer, evidence.

Finding 8-10 (malformed input, caps, audit append-only, principals, pragmas)
live in ``test_keeper_hardening_admin.py``; the file was split to stay under
400 lines.
"""
import random
import sqlite3
import threading

import pytest

from keeper_util import World, defn, seed, to_claimed, to_in_review
from plangraph.keeper import auth, db as kdb
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


STORE_LAYER = "store layer, next task"


# =============================================================== finding 5

def test_guarded_update_refuses_state_changed_under_it(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    conn2 = kdb.open_db(w.path)
    try:
        store2 = Store(conn2, clock=w.clock, verifiers=w.verifiers)
        original = store2._require_evidence

        def hook(item):
            original(item)
            conn2.execute(
                "UPDATE items SET state='open', claimed_by=NULL, epoch=?"
                " WHERE key=?", (item["epoch"] + 1, key))

        store2._require_evidence = hook
        with pytest.raises(Refused) as exc:
            store2.submit(w.p("l1main"), key, epoch)
        assert exc.value.code == "conflict"
    finally:
        conn2.close()
    item = w.store._item(key)
    assert (item["state"], item["epoch"]) == ("claimed", epoch)


def test_two_stores_cannot_double_submit(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    conn2 = kdb.open_db(w.path)
    try:
        store2 = Store(conn2, clock=w.clock, verifiers=w.verifiers)
        principals = [auth.authenticate(w.conn, w.tok("l1main")),
                      auth.authenticate(conn2, w.tok("l1main"))]
        barrier = threading.Barrier(2)
        outcomes = []

        def run(store, principal):
            barrier.wait()
            try:
                store.submit(principal, key, epoch)
                outcomes.append("ok")
            except Refused as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=run, args=(store, principal))
                   for store, principal in zip((w.store, store2), principals)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert outcomes.count("ok") == 1
        assert all(code == "bad_state" for code in outcomes if code != "ok")
        assert w.store._item(key)["state"] == "in_review"
    finally:
        conn2.close()


def test_release_then_reclaim_stales_the_old_epoch(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    epoch1 = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch1, "decision", {"id": "D-1"})
    w.store.release(w.p("l1main"), key, epoch1)
    epoch2 = to_claimed(w, key)
    assert epoch2 == epoch1 + 1
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), key, epoch1)
    assert exc.value.code == "epoch_stale"


# =============================================================== finding 6

def test_l1_cannot_cancel_with_active_dependent(w):
    (key,) = seed(w, [("A", [])])          # demo:GOAL depends on demo:A
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p("l1main"), key, "drop")
    assert exc.value.code == "conflict"
    assert w.store._item(key)["state"] == "open"


def test_router_can_cancel_with_active_dependent(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    assert w.store._item(key)["state"] == "cancelled"


def test_create_in_unrelated_project_not_blocked_by_old_problem(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")   # leaves demo:GOAL uncancellable-ish
    items = [defn("Z", project="fresh"),
             defn("GOAL", project="fresh", kind="goal", deps=["Z"])]
    w.store.create_items(w.p("router"), items)
    assert w.store._item("fresh:Z")["state"] == "open"


def test_edit_unrelated_item_not_blocked_by_old_problem(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    items = [defn("Z", project="fresh"),
             defn("GOAL", project="fresh", kind="goal", deps=["Z"])]
    w.store.create_items(w.p("router"), items)
    w.store.edit_definition(w.p("router"), "fresh:Z", {"title": "renamed"})
    assert w.store._item("fresh:Z")["title"] == "renamed"


def test_import_in_unrelated_project_not_blocked_by_old_problem(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    items = [
        {"project": "fresh", "id": "Z", "title": "z", "kind": "lane",
         "state": "open", "depends_on": [], "evidence_kinds": ["any"]},
        {"project": "fresh", "id": "GOAL", "title": "g", "kind": "goal",
         "state": "open", "depends_on": ["fresh:Z"], "evidence_kinds": ["any"]},
    ]
    w.store.import_items(w.p("router"), items)
    assert w.store._item("fresh:Z")["state"] == "open"


def test_l1_cross_project_depends_on_refused(w):
    seed(w, [("A", [])], project="other")
    items = [defn("B", deps=["other:A"]),
             defn("GOAL", kind="goal", deps=["B"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("l1main"), items)
    assert exc.value.code == "forbidden"


# =============================================================== finding 7

def test_fold_clears_stale_fields_on_second_changes(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, False, "one")
    w.store.submit(w.p("l1main"), key, epoch)
    w.store.verdict(w.p("reviewer"), key, False, "two")
    assert w.store.verify_integrity() == []


def test_heartbeat_preserves_integrity(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    w.clock.advance(10)
    w.store.heartbeat(w.p("l1main"), key, epoch)
    assert w.store.verify_integrity() == []


def test_integrity_detects_evidence_verified_tamper(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    raw = sqlite3.connect(w.path)
    raw.execute("UPDATE evidence SET verified=0 WHERE key=?", (key,))
    raw.commit()
    raw.close()
    problems = w.store.verify_integrity()
    assert any(p["code"] == "evidence_mismatch" for p in problems)


def _walk(w, rng, steps):
    keys = seed(w, [("A", []), ("B", []), ("C", [])])
    for _ in range(steps):
        key = rng.choice(keys)
        item = w.store._item(key)
        state = item["state"]
        if state == "open":
            action = rng.choice(["claim", "edit", "cancel", "claim"])
            if action == "claim":
                try:
                    w.store.claim(w.p("l1main"), key)
                except Refused:
                    pass
            elif action == "edit":
                try:
                    w.store.edit_definition(w.p("router"), key,
                                            {"note": f"n{rng.randint(0, 9)}"})
                except Refused:
                    pass
            else:
                try:
                    w.store.cancel(w.p("router"), key, "walk")
                except Refused:
                    pass
        elif state == "claimed":
            epoch = item["epoch"]
            action = rng.choice(["evidence", "heartbeat", "release", "submit"])
            if action == "evidence":
                w.store.add_evidence(w.p("l1main"), key, epoch, "decision",
                                     {"id": f"D-{rng.randint(1, 99)}"})
            elif action == "heartbeat":
                w.store.heartbeat(w.p("l1main"), key, epoch)
            elif action == "release":
                w.store.release(w.p("l1main"), key, epoch)
            else:
                try:
                    w.store.submit(w.p("l1main"), key, epoch)
                except Refused:
                    pass
        elif state == "in_review":
            try:
                w.store.verdict(w.p("reviewer"), key,
                                rng.random() < 0.5, "walk")
            except Refused:
                pass
        w.clock.advance(1)


def test_random_walk_of_valid_verbs_keeps_integrity(w):
    _walk(w, random.Random(20261001), 80)
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_long_random_walk_keeps_integrity(w):
    """200 valid verbs through the Store API end with a clean cache.

    Fixed seed, so a failure is reproducible.  Non-strict xfail: if this trips
    it is because the store still fails to emit an event for a verb (outside
    this schema/events layer), not because the fold/verify side is wrong.
    """
    _walk(w, random.Random(0x5EED), 200)
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []
