"""KEEPER-FLOOR-LEGACY-CLAIM A2 slice 1 (D-3066): the router ``waive_floor`` verb.

A legacy row (an old claim whose release/re-claim carries no recorded
``payload.base``) can never close: ``_bind_floor`` fails closed and ``commit+ci``
refuses "no ancestry floor recorded ...".  The router-only ``waive_floor`` verb
appends a ``floor_waived`` event that either lifts the floor requirement or
pins it to an explicit ``sha_floor``; any later release or foreign claim
supersedes the waiver by being the newest floor event.  Nothing else moves:
no state, no epoch, no token revocation — and the merged-PR/trailer/CI checks
all still run.

Helpers come from ``keeper_util``/``evidence_fakes`` and the pinned
``test_keeper_evidence_wiring`` (its ``RecGit``), so nothing here touches git,
the network or the filesystem.
"""
from __future__ import annotations

import json

import pytest

from evidence_fakes import CLAIM, SHA, FakeGit, FakeRuns, make_ctx
from keeper_util import World, seed, to_claimed
from plangraph.keeper import events
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store
from plangraph.keeper.store_support import iso
from test_keeper_evidence_wiring import RecGit

FLOOR_A = "e" * 40

ITEM_COLUMNS = ("state", "epoch", "round", "claimed_by", "delegate",
                "reviewer", "lease_until", "review_waiver")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    # Real verifiers, fakes for every adapter (as test_keeper_evidence_wiring).
    world.store = Store(world.conn, clock=world.clock, ctx=make_ctx())
    yield world
    world.close()


def legacy_row(w, key, who="workerA"):
    """The D-3066 shape: a release with no recorded base, then a re-claim.

    Mirrors ``test_release_without_base_then_reclaim_requires_floor_and_fails``:
    the release runs with no git adapter so its payload carries no ``base``,
    and the re-claim's own claim event is the claimant's (never a floor), so
    ``_bind_floor`` fails closed on the unrecorded release.  Returns the
    re-claim's epoch; the fake git carries the correct ``Plan-Item`` trailer.
    """
    epoch1 = to_claimed(w, key, who="l1main")
    w.store.ctx = make_ctx(git=None)
    w.store.release(w.p("l1main"), key, epoch1)
    w.store.ctx = make_ctx(git=RecGit(trailers={"Plan-Item": key}))
    return to_claimed(w, key, who=who)


def _item_tuple(w, key):
    return tuple(w.conn.execute(
        f"SELECT {','.join(ITEM_COLUMNS)} FROM items WHERE key=?", (key,)).fetchone())


def test_fire_legacy_row_waiver_unblocks_commit_ci(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)

    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "no ancestry floor" in out["detail"]

    res = w.store.waive_floor(w.p("router"), key, "D-3066")
    assert res == {"key": key, "decision": "D-3066", "sha_floor": None}

    # Same add_evidence, same RecGit (trailers Plan-Item = key): now verified.
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is True


@pytest.mark.parametrize("descends", [True, False])
def test_waiver_with_sha_floor_runs_the_descend_check(w, descends):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)

    w.store.waive_floor(w.p("router"), key, "D-3066", sha_floor=FLOOR_A)

    git = RecGit(descends=descends, trailers={"Plan-Item": key})
    w.store.ctx = make_ctx(git=git)
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert (FLOOR_A, SHA) in git.calls          # floor check ran against it
    if descends:
        assert out["verified"] is True
    else:
        assert out["verified"] is False
        assert "does not descend" in out["detail"]


def test_release_after_the_waiver_re_arms_the_floor(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066")

    # A later release is then the newest floor event: waiver superseded.
    w.store.ctx = make_ctx(git=None)
    w.store.release(w.p("workerA"), key, epoch)
    w.store.ctx = make_ctx(git=RecGit(trailers={"Plan-Item": key}))
    epoch2 = to_claimed(w, key, who="workerA")

    out = w.store.add_evidence(w.p("workerA"), key, epoch2, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "no ancestry floor" in out["detail"]


def test_foreign_claim_after_the_waiver_re_arms_the_floor(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)                   # claimed by workerA
    w.store.waive_floor(w.p("router"), key, "D-3066")

    # A foreign claim (workerB) after the waiver is the newest floor event.
    events.append_event(w.conn, iso(CLAIM), "workerB", "worker", None, key,
                        "claimed", {"epoch": epoch, "claimed_by": "workerB"})
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "no ancestry floor" in out["detail"]


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer"])
def test_only_router_can_waive(w, who):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    legacy_row(w, key)

    with pytest.raises(Refused) as exc_info:
        w.store.waive_floor(w.p(who), key, "D-3066")
    assert exc_info.value.code == "forbidden"
    assert w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='floor_waived'").fetchone()[0] == 0


@pytest.mark.parametrize("decision",
                         ["3066", "d-3066", "D-x", "D-3066x", "D-3066 ",
                          "D-", "", None,
                          "D-\u0663\u0660\u0666\u0663",   # Arabic-Indic digits
                          "D-\u07c8"])                      # NKO digit
def test_bad_decision_id_is_a_validation_refusal(w, decision):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    legacy_row(w, key)

    with pytest.raises(Refused) as exc_info:
        w.store.waive_floor(w.p("router"), key, decision)
    assert exc_info.value.code == "validation"
    assert w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='floor_waived'").fetchone()[0] == 0
    assert w.store.verify_chain() == []


@pytest.mark.parametrize("sha_floor",
                         ["xyz", "A" * 40, "f" * 39, "f" * 41, 42])
def test_bad_sha_floor_is_a_validation_refusal(w, sha_floor):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    legacy_row(w, key)

    with pytest.raises(Refused) as exc_info:
        w.store.waive_floor(w.p("router"), key, "D-3066", sha_floor=sha_floor)
    assert exc_info.value.code == "validation"
    assert w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='floor_waived'").fetchone()[0] == 0
    assert w.store.verify_chain() == []


def test_waiver_does_not_skip_trailer_or_pr_checks(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066")

    # Wrong Plan-Item trailer: still refused under the waiver.
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": "demo:OTHER"}))
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "Plan-Item" in out["detail"]

    # No merged PR: still refused under the waiver.
    w.store.ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": key}, merge=None))
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "merged PR" in out["detail"]


def test_waiver_is_stateless_and_revokes_no_tokens(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)
    w.store.ctx = make_ctx(runs=FakeRuns(job={"request": {"item_key": key}}))
    w.store.mint_run_token(w.p("l1main"), key, "writer", "run-1")

    before = _item_tuple(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066")
    after = _item_tuple(w, key)

    assert after == before                     # no state, epoch or holder moved
    assert after[ITEM_COLUMNS.index("state")] == "claimed"
    assert after[ITEM_COLUMNS.index("epoch")] == epoch
    row = w.conn.execute(
        "SELECT revoked_at FROM run_tokens WHERE run_id='run-1'").fetchone()
    assert row is not None and row["revoked_at"] is None
    assert w.store.verify_integrity() == []    # fold + token replay agree


def test_replay_and_fold_stay_clean_with_floor_waived(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066", sha_floor=FLOOR_A)

    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []
    folded = events.fold(events.read_events(w.conn))
    assert folded[key]["state"] == "claimed"
    assert folded[key]["epoch"] == epoch
    row = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='floor_waived'",
        (key,)).fetchone()
    assert json.loads(row["payload"]) == {"decision": "D-3066",
                                         "sha_floor": FLOOR_A,
                                         "epoch": epoch}


def test_waive_refused_when_a_valid_floor_is_recorded(w):
    """F3: a recorded floor base is provenance the waiver may not erase."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)

    # A release with a recorded base makes _bind_floor answer (base, True).
    w.store.ctx = make_ctx(git=FakeGit(head=FLOOR_A, trailers={"Plan-Item": key}))
    w.store.release(w.p("workerA"), key, epoch)
    assert w.store._bind_floor(w.store._item(key)) == (FLOOR_A, True)

    with pytest.raises(Refused) as exc_info:
        w.store.waive_floor(w.p("router"), key, "D-3066")
    assert exc_info.value.code == "bad_state"
    assert w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='floor_waived'").fetchone()[0] == 0


def test_pinned_sha_floor_may_not_be_waived_away(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    legacy_row(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066", sha_floor=FLOOR_A)
    assert w.store._bind_floor(w.store._item(key)) == (FLOOR_A, True)

    with pytest.raises(Refused) as exc_info:
        w.store.waive_floor(w.p("router"), key, "D-3067")
    assert exc_info.value.code == "bad_state"


@pytest.mark.parametrize("payload", ['[]', '"floor"', '{"decision": "D-3066"}',
                                     '{"sha_floor": null}', '{"epoch": 2}',
                                     '{"decision": "D-3066", "sha_floor": null}',
                                     '{"decision": "D-3066", "epoch": 2}'])
def test_malformed_waiver_payload_fails_closed(w, payload):
    """F1: anything but the recorded payload shape never lifts the floor."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = legacy_row(w, key)
    w.store.waive_floor(w.p("router"), key, "D-3066")
    assert w.store._bind_floor(w.store._item(key)) == (None, False)

    w.conn.execute("UPDATE events SET payload=? WHERE key=? AND type='floor_waived'",
                   (payload, key))
    w.conn.commit()
    assert w.store._bind_floor(w.store._item(key)) == (None, True)   # fail closed

    w.store.ctx = make_ctx(git=RecGit(trailers={"Plan-Item": key}))
    out = w.store.add_evidence(w.p("workerA"), key, epoch, "commit+ci",
                               {"commit": SHA})
    assert out["verified"] is False
    assert "no ancestry floor" in out["detail"]


def test_bind_floor_shapes(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    item = w.store._item(key)
    assert w.store._bind_floor(item) == (None, False)   # first claim: no floor

    legacy_row(w, key)
    item = w.store._item(key)
    assert w.store._bind_floor(item) == (None, True)    # unrecorded: fail closed

    w.store.waive_floor(w.p("router"), key, "D-3066")
    assert w.store._bind_floor(w.store._item(key)) == (None, False)   # waived

    w.store.waive_floor(w.p("router"), key, "D-3067", sha_floor=FLOOR_A)
    assert w.store._bind_floor(w.store._item(key)) == (FLOOR_A, True)  # pinned
