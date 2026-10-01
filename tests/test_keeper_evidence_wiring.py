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


def test_commit_before_claim_is_unverified_and_blocks_submit(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key},
                                       commit_time=CLAIM - 1000))
    epoch = to_claimed(w, key)

    out = w.store.add_evidence(w.p("l1main"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), key, epoch)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(key)["state"] == "claimed"


def test_decision_item_end_to_end(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
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
