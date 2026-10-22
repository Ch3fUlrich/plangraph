"""Store <-> real verifier wiring (Task B0).

The store must default to :data:`plangraph.keeper.evidence.VERIFIERS` and hand
each verifier a context whose ``claim_time`` is the item's real claim instant.
The I/O adapters come from ``evidence_fakes``, so nothing here touches git, the
network or the filesystem.
"""
from __future__ import annotations

import json

import pytest

from evidence_fakes import CLAIM, SHA, FakeGit, make_ctx
from keeper_util import World, seed, to_claimed
from plangraph.keeper import events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence import VERIFIERS
from plangraph.keeper.store import Store
from plangraph.keeper.store_support import iso


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


FLOOR_A = "e" * 40
FLOOR_B = "f" * 40


class RecGit(FakeGit):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, str]] = []

    def is_ancestor(self, repo: str, sha: str, ref: str) -> bool:
        self.calls.append((sha, ref))
        return super().is_ancestor(repo, sha, ref)


def test_first_claim_without_floor_event_verifies_and_submits(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key}))
    epoch = to_claimed(w, key)

    out = w.store.add_evidence(
        w.p("l1main"), key, epoch, "commit+ci", {"commit": SHA}
    )
    assert out["verified"] is True
    w.store.submit(w.p("l1main"), key, epoch)
    assert w.store._item(key)["state"] == "in_review"


def test_claim_and_release_payloads_carry_base(w):
    w.store.ctx = make_ctx(git=FakeGit(head=FLOOR_A))
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = to_claimed(w, key)
    w.store.release(w.p("l1main"), key, epoch)

    claimed_rows = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type=? ORDER BY seq",
        (key, "claimed"),
    ).fetchall()
    released_rows = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type=? ORDER BY seq",
        (key, "released"),
    ).fetchall()

    assert json.loads(claimed_rows[0]["payload"])["base"] == FLOOR_A
    assert json.loads(released_rows[0]["payload"])["base"] == FLOOR_A
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_claim_succeeds_without_base_when_git_or_head_unavailable(w):
    class GitWithoutHead:
        pass

    cases = [
        make_ctx(git=None),
        make_ctx(git=GitWithoutHead()),
        make_ctx(git=FakeGit(head_error=RuntimeError("x"))),
    ]
    keys = seed(
        w,
        [("A", []), ("B", []), ("C", [])],
        evidence_kinds=("commit+ci",),
    )

    assert len(keys) == len(cases)
    for ctx, key in zip(cases, keys):
        w.store.ctx = ctx
        epoch = to_claimed(w, key)
        assert epoch == 1
        row = w.conn.execute(
            "SELECT payload FROM events WHERE key=? AND type=? ORDER BY seq DESC LIMIT 1",
            (key, "claimed"),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert "base" not in payload


def test_release_by_l1main_then_reclaim_by_workera_uses_released_base(w):
    k_ok, k_fail = seed(
        w,
        [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",),
    )

    for key, descends, should_verify in [
        (k_ok, True, True),
        (k_fail, False, False),
    ]:
        w.store.ctx = make_ctx(git=FakeGit(head=FLOOR_A))
        epoch1 = to_claimed(w, key, who="l1main")
        w.store.ctx = make_ctx(git=FakeGit(head=FLOOR_B))
        w.store.release(w.p("l1main"), key, epoch1)

        git = RecGit(descends=descends, trailers={"Plan-Item": key})
        w.store.ctx = make_ctx(git=git)
        epoch2 = to_claimed(w, key, who="workerA")

        out = w.store.add_evidence(
            w.p("workerA"), key, epoch2, "commit+ci", {"commit": SHA}
        )
        assert (FLOOR_B, SHA) in git.calls

        if should_verify:
            assert out["verified"] is True
            w.store.submit(w.p("workerA"), key, epoch2)
            assert w.store._item(key)["state"] == "in_review"
        else:
            assert out["verified"] is False
            assert "does not descend" in out["detail"]
            with pytest.raises(Refused) as exc_info:
                w.store.submit(w.p("workerA"), key, epoch2)
            assert exc_info.value.code == "evidence_unverified"
            assert w.store._item(key)["state"] == "claimed"


def test_foreign_claim_event_sets_floor(w):
    k1, k2, k3 = seed(
        w,
        [("A", []), ("B", []), ("C", [])],
        evidence_kinds=("commit+ci",),
    )

    # 1. Foreign claim with recorded base and valid ancestry -> verified
    epoch1 = to_claimed(w, k1, who="l1main")
    events.append_event(
        w.conn,
        iso(CLAIM),
        "workerA",
        "worker",
        None,
        k1,
        "claimed",
        {"epoch": 1, "claimed_by": "workerA", "base": FLOOR_A},
    )
    git1 = RecGit(descends=True, trailers={"Plan-Item": k1})
    w.store.ctx = make_ctx(git=git1)
    out1 = w.store.add_evidence(
        w.p("l1main"), k1, epoch1, "commit+ci", {"commit": SHA}
    )
    assert out1["verified"] is True
    assert (FLOOR_A, SHA) in git1.calls

    # 2. Foreign claim with recorded base but non-descending commit -> unverified
    epoch2 = to_claimed(w, k2, who="l1main")
    events.append_event(
        w.conn,
        iso(CLAIM),
        "workerA",
        "worker",
        None,
        k2,
        "claimed",
        {"epoch": 1, "claimed_by": "workerA", "base": FLOOR_A},
    )
    git2 = RecGit(descends=False, trailers={"Plan-Item": k2})
    w.store.ctx = make_ctx(git=git2)
    out2 = w.store.add_evidence(
        w.p("l1main"), k2, epoch2, "commit+ci", {"commit": SHA}
    )
    assert out2["verified"] is False
    assert "does not descend" in out2["detail"]
    assert (FLOOR_A, SHA) in git2.calls

    # 3. Foreign claim without recorded base -> unverified (no floor)
    epoch3 = to_claimed(w, k3, who="l1main")
    events.append_event(
        w.conn,
        iso(CLAIM),
        "workerA",
        "worker",
        None,
        k3,
        "claimed",
        {"epoch": 1, "claimed_by": "workerA"},
    )
    git3 = RecGit(trailers={"Plan-Item": k3})
    w.store.ctx = make_ctx(git=git3)
    out3 = w.store.add_evidence(
        w.p("l1main"), k3, epoch3, "commit+ci", {"commit": SHA}
    )
    assert out3["verified"] is False
    assert "no ancestry floor" in out3["detail"]


def test_release_without_base_then_reclaim_requires_floor_and_fails(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch1 = to_claimed(w, key, who="l1main")

    w.store.ctx = make_ctx(git=None)
    w.store.release(w.p("l1main"), key, epoch1)

    w.store.ctx = make_ctx(git=RecGit(trailers={"Plan-Item": key}))
    epoch2 = to_claimed(w, key, who="workerA")

    out = w.store.add_evidence(
        w.p("workerA"), key, epoch2, "commit+ci", {"commit": SHA}
    )
    assert out["verified"] is False
    assert "no ancestry floor" in out["detail"]


def test_current_claimant_own_claim_is_not_a_floor(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    git = RecGit(trailers={"Plan-Item": key})
    w.store.ctx = make_ctx(git=git)
    epoch = to_claimed(w, key, who="l1main")

    out = w.store.add_evidence(
        w.p("l1main"), key, epoch, "commit+ci", {"commit": SHA}
    )
    assert out["verified"] is True
    assert (SHA, "main") in git.calls
    assert not any(len(ref) == 40 for _, ref in git.calls)


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
