"""Store <-> real verifier wiring (Task B0).

The store must default to :data:`plangraph.keeper.evidence.VERIFIERS` and hand
each verifier a context whose ``claim_time`` is the item's real claim instant.
The I/O adapters come from ``evidence_fakes``, so nothing here touches git, the
network or the filesystem.
"""
from __future__ import annotations

import pytest

from evidence_fakes import CLAIM, SHA, FakeGit, make_ctx
from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence import VERIFIERS
from plangraph.keeper.store import Store


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    # Real verifiers, fakes for every adapter.
    world.store = Store(world.conn, clock=world.clock, ctx=make_ctx())
    yield world
    world.close()


def test_store_defaults_to_real_verifiers(w):
    assert set(w.store.verifiers) == set(VERIFIERS)
    assert w.store.verifiers["decision"] is VERIFIERS["decision"]


def test_real_verifier_sees_the_stores_claim_time(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key}))
    epoch = to_claimed(w, key)

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is True
    w.store.submit(w.p("l1main"), key, epoch)
    assert w.store._item(key)["state"] == "in_review"


def test_trailer_commit_predating_first_claim_is_verified(w):
    # F1: a first-time claim has no release/re-claim floor, so a trailer-bound
    # commit older than the claim is bound by its trailer and verifies.
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM - 1000))
    epoch = to_claimed(w, key)

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is True, out["detail"]
    w.store.submit(w.p("l1main"), key, epoch)
    assert w.store._item(key)["state"] == "in_review"


def test_commit_after_last_release_before_reclaim_is_verified(w):
    # claim by l1main at CLAIM, release at CLAIM+300, re-claim by workerA at
    # CLAIM+600.  The floor is the release (+300), not the current claim: a
    # commit at +400 predates the claim but binds.
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = to_claimed(w, key)
    w.clock.advance(300)
    w.store.release(w.p("l1main"), key, epoch)
    w.clock.advance(300)
    epoch2 = to_claimed(w, key, who="workerA")
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM + 400))

    out = w.store.add_evidence(w.p("workerA"), key, epoch2, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is True, out["detail"]
    w.store.submit(w.p("workerA"), key, epoch2)
    assert w.store._item(key)["state"] == "in_review"


def test_commit_predating_last_release_is_unverified(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = to_claimed(w, key)
    w.clock.advance(300)
    w.store.release(w.p("l1main"), key, epoch)
    w.clock.advance(300)
    epoch2 = to_claimed(w, key, who="workerA")
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM + 100))

    out = w.store.add_evidence(w.p("workerA"), key, epoch2, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "release" in out["detail"]
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("workerA"), key, epoch2)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(key)["state"] == "claimed"


def test_commit_after_foreign_claim_floor_is_verified(w):
    # No release anywhere: the floor is the most recent claim by a different
    # principal (workerA at CLAIM), not the current claim (l1main at +200).
    # A commit at +50 predates the current claim's window but postdates the
    # floor, so it binds.
    from plangraph.keeper import events
    from plangraph.keeper.store_support import iso
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    events.append_event(w.conn, iso(CLAIM), "workerA", "worker", None, key,
                        "claimed", {"epoch": 1, "claimed_by": "workerA"})
    w.clock.advance(200)
    epoch = to_claimed(w, key)
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM + 50))

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is True, out["detail"]


def test_commit_predating_foreign_claim_floor_is_unverified(w):
    from plangraph.keeper import events
    from plangraph.keeper.store_support import iso
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    events.append_event(w.conn, iso(CLAIM), "workerA", "worker", None, key,
                        "claimed", {"epoch": 1, "claimed_by": "workerA"})
    w.clock.advance(200)
    epoch = to_claimed(w, key)
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM - 200))

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "re-claim" in out["detail"]


def test_decision_item_end_to_end(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",), review_tier="high")
    epoch = to_claimed(w, key)

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "decision",
                               {"id": "D-209"})
    assert out["verified"] is True
    w.store.submit(w.p("l1main"), key, epoch)
    assert w.store._item(key)["state"] == "in_review"

    # reviewer is family teamC, claimant l1main is teamA: cross-family review.
    # D-219 needs two independent reviewers; the first verdict keeps it in
    # review and reviewer2 (family teamR) has to add the second.
    w.store.verdict(w.p("reviewer"), key, True, "cross-family review")
    assert w.store._item(key)["state"] == "in_review"
    w.store.verdict(w.p("reviewer2"), key, True, "second family")
    assert w.store._item(key)["state"] == "done"


def test_unreadable_event_stamp_fails_closed(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = to_claimed(w, key)
    w.clock.advance(300)
    w.store.release(w.p("l1main"), key, epoch)
    epoch2 = to_claimed(w, key, who="workerA")
    item = dict(w.store._item(key))
    real = w.conn.execute
    class Conn:
        def __getattr__(self, name):
            return getattr(w.conn, name)
        def execute(self, sql, params=()):
            cur = real(sql, params)
            if "type='released'" in sql:
                return type("C", (), {"fetchone": lambda s: {"ts": "garbage"}})()
            return cur
    w.store.conn = Conn()
    try:
        assert w.store._bind_floor_time(item) == float("inf")
    finally:
        del w.store.conn
