"""Final-C hardening: the last four keeper-store defects.

Four items:

1. ``verdict(ok=False)`` re-reads the row inside the write transaction but does
   not re-run the checks the ok=True path applies to that fresh row.  A reviewer
   whose item was moved out of scope, or delegated onto a peer, between the
   pre-read and the transaction can still request changes.  Mirror the checks.
2. ``set_priority`` reads and ``_assert_unlocked`` before ``BEGIN`` and then
   updates blindly, so a concurrently cancelled/DONE item is touched (the lock
   triggers rescue the data, but with the wrong refusal and no guard).  Read,
   assert and update inside the transaction under a rowcount guard.
3. ``cancel`` predicates the update on ``state`` only, so a release+reclaim
   that lands in the same state at a new epoch is still cancelled.  Add
   ``AND epoch=?`` with the epoch read inside the transaction.
4. A ``changes_requested`` that returns the item to ``claimed`` leaves
   ``lease_until`` NULL, so the reaper never reclaims a claimant who dies
   while reworking.  Set a fresh default lease and carry it in the event so
   ``events.fold`` and ``verify_integrity`` agree.
"""
from __future__ import annotations

import pytest

from keeper_util import World, seed, to_claimed, to_in_review
from plangraph.keeper import auth, db as kdb
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _arm(store, competitor):
    """Run *competitor* once, just before *store*'s next write transaction."""
    original = store._write

    def hooked():
        competitor()
        return original()

    store._write = hooked
    return original


def _second(world):
    conn = kdb.open_db(world.path)
    store = Store(conn, clock=world.clock, verifiers=world.verifiers)
    return store, conn


def _count(world, key, kind):
    return world.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type=?",
        (key, kind)).fetchone()[0]


# ============================================================== item 1
# ================================== verdict(changes) fresh-row rechecks

def test_verdict_changes_item_rescoped_away_refused(w):
    """The item moved to a project the reviewer is not scoped to."""
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    store2, conn2 = _second(w)
    try:
        def competitor():
            conn2.execute("UPDATE items SET project='other' WHERE key=?", (key,))
            conn2.commit()

        _arm(w.store, competitor)
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p("reviewer"), key, False, "changes")
        assert exc.value.code == "forbidden"
        assert w.store._item(key)["state"] == "in_review"
        assert _count(w, key, "changes_requested") == 0
    finally:
        conn2.close()


def test_verdict_changes_reviewer_descoped_refused(w):
    """The reviewer principal loses the project after the pre-read.

    A re-scope cannot go through the principals table (the revoke-only trigger
    forbids editing ``projects``), so the race is modelled by mutating the
    authenticated principal between the pre-read and the write transaction.
    """
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    reviewer = w.p("reviewer")
    store2, conn2 = _second(w)
    try:
        def competitor():
            reviewer.projects = []

        _arm(w.store, competitor)
        with pytest.raises(Refused) as exc:
            w.store.verdict(reviewer, key, False, "changes")
        assert exc.value.code == "forbidden"
        assert w.store._item(key)["state"] == "in_review"
        assert _count(w, key, "changes_requested") == 0
    finally:
        conn2.close()


def test_verdict_changes_delegated_to_reviewer_refused(w):
    """The item was delegated onto the reviewer between the reads.

    S2-4: the holder-identity re-check fires first, so a delegate changed
    under the transaction is ``conflict`` (the mirrored same_principal
    check no longer runs for this race).  Still refused, still in_review,
    still no event.
    """
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    store2, conn2 = _second(w)
    try:
        def competitor():
            conn2.execute("UPDATE items SET delegate='reviewer' WHERE key=?", (key,))
            conn2.commit()

        _arm(w.store, competitor)
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p("reviewer"), key, False, "changes")
        assert exc.value.code == "conflict"
        assert w.store._item(key)["state"] == "in_review"
        assert _count(w, key, "changes_requested") == 0
    finally:
        conn2.close()


def test_verdict_changes_delegate_same_family_refused(w):
    """A delegate in the reviewer's family makes the review non-independent.

    S2-4: the holder-identity re-check fires first, so a delegate changed
    under the transaction is ``conflict`` (the mirrored same_family check
    no longer runs for this race).  Still refused, still in_review, still
    no event.
    """
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    store2, conn2 = _second(w)
    try:
        def competitor():
            conn2.execute("UPDATE items SET delegate='workerC' WHERE key=?", (key,))
            conn2.commit()

        _arm(w.store, competitor)
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p("reviewer"), key, False, "changes")
        assert exc.value.code == "conflict"
        assert w.store._item(key)["state"] == "in_review"
        assert _count(w, key, "changes_requested") == 0
    finally:
        conn2.close()


# ============================================================== item 2
# ============================================= set_priority transaction

def test_set_priority_race_cancelled_refused_no_event(w):
    (key,) = seed(w, [("A", [])])
    store2, conn2 = _second(w)
    try:
        router2 = auth.authenticate(conn2, w.tok("router"))
        _arm(w.store, lambda: store2.cancel(router2, key, "race"))
        with pytest.raises(Refused) as exc:
            w.store.set_priority(w.p("router"), key, 99)
        assert exc.value.code == "locked"
        item = w.store._item(key)
        assert item["state"] == "cancelled"
        assert item["priority"] == 0
        assert _count(w, key, "priority_set") == 0
    finally:
        conn2.close()


def test_set_priority_race_done_refused_no_event(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    w.store.verdict(w.p("reviewer2"), key, True, "first")  # D-219: one of two
    store2, conn2 = _second(w)
    try:
        reviewer2 = auth.authenticate(conn2, w.tok("reviewer"))
        _arm(w.store, lambda: store2.verdict(reviewer2, key, True, "ok"))
        with pytest.raises(Refused) as exc:
            w.store.set_priority(w.p("router"), key, 99)
        assert exc.value.code == "locked"
        item = w.store._item(key)
        assert item["state"] == "done"
        assert item["priority"] == 0
        assert _count(w, key, "priority_set") == 0
    finally:
        conn2.close()


# ============================================================== item 3
# ======================================================= cancel epoch guard

def test_cancel_race_release_reclaim_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    store2, conn2 = _second(w)
    try:
        other = auth.authenticate(conn2, w.tok("l1main"))

        def competitor():
            store2.release(other, key, epoch)
            store2.claim(other, key)

        _arm(w.store, competitor)
        with pytest.raises(Refused) as exc:
            w.store.cancel(w.p("router"), key, "race")
        assert exc.value.code == "conflict"
        item = w.store._item(key)
        assert item["state"] == "claimed"
        assert item["epoch"] == epoch + 1
        assert _count(w, key, "cancelled") == 0
    finally:
        conn2.close()


# ============================================================== item 4
# ============================================ changes-requested lease

def test_changes_requested_sets_lease_then_reaps(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    result = w.store.verdict(w.p("reviewer"), key, False, "changes")
    assert result["state"] == "claimed"
    item = w.store._item(key)
    assert item["lease_until"] == w.store._iso(
        w.clock() + w.store.default_lease)
    assert w.store.verify_integrity() == []
    # A claimant who dies while reworking is reclaimed once the lease lapses.
    w.clock.advance(w.store.default_lease + 10)
    reaped = w.store.reap(w.p("system"), w.clock())
    assert key in reaped["expired"]
    assert w.store._item(key)["state"] == "open"
    assert w.store.verify_integrity() == []


def test_heartbeat_after_changes_extends_lease(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, False, "changes")
    first = w.store._item(key)["lease_until"]
    w.clock.advance(100)
    w.store.heartbeat(w.p("l1main"), key, epoch)
    second = w.store._item(key)["lease_until"]
    assert second == w.store._iso(w.clock() + w.store.default_lease)
    assert second > first
    assert w.store.verify_integrity() == []
