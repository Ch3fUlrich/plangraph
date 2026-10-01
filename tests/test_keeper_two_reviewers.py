"""D-219 two-reviewer gate.

One implementer plus exactly two independent reviewers: a passing verdict
records a ``review_verdict`` and leaves the item in review; only the second
passing verdict, from a different principal of a different family, emits
``review_passed`` and locks the item.  A ``--changes`` verdict from either seat
returns the item to ``claimed`` and voids that round's earlier verdicts.
"""
import pytest

from keeper_util import World, pass_review, seed, to_in_review
from plangraph.keeper import auth
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


KEY = "demo:A"


def _count(world, key, event_type):
    return world.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type=?",
        (key, event_type)).fetchone()[0]


def test_single_verdict_stays_in_review(w):
    to_in_review(w, KEY)
    result = w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    assert result["state"] == "in_review"
    assert w.store._item(KEY)["state"] == "in_review"
    assert _count(w, KEY, "review_verdict") == 1
    assert _count(w, KEY, "review_passed") == 0
    assert w.store.verify_integrity() == []


def test_second_verdict_from_same_family_refused(w):
    to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")  # family teamC
    token = auth.add_principal(w.conn, "reviewer3", "reviewer", ["demo"],
                               family="teamC")
    reviewer3 = auth.authenticate(w.conn, token)
    with pytest.raises(Refused) as exc:
        w.store.verdict(reviewer3, KEY, True, "cozy")
    assert exc.value.code == "same_family"
    assert w.store._item(KEY)["state"] == "in_review"
    assert _count(w, KEY, "review_passed") == 0


def test_second_reviewer_other_family_reaches_done(w):
    to_in_review(w, KEY)
    first = w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    assert first["state"] == "in_review"
    second = w.store.verdict(w.p("reviewer2"), KEY, True, "agreed")
    assert second["state"] == "done"
    item = w.store._item(KEY)
    assert item["state"] == "done" and item["reviewer"] == "reviewer2"
    assert _count(w, KEY, "review_verdict") == 2
    assert _count(w, KEY, "review_passed") == 1
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_same_reviewer_twice_refused(w):
    to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), KEY, True, "ok again")
    assert exc.value.code == "same_principal"
    assert w.store._item(KEY)["state"] == "in_review"


def test_third_verdict_refused_and_done_is_locked(w):
    to_in_review(w, KEY)
    pass_review(w, KEY)
    assert w.store._item(KEY)["state"] == "done"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), KEY, True, "once more")
    assert exc.value.code in ("locked", "conflict")
    assert _count(w, KEY, "review_passed") == 1
    # ``done`` freezes the other write surfaces too.
    with pytest.raises(Refused) as exc:
        w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-X"})
    assert exc.value.code == "locked"
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), KEY)
    assert exc.value.code == "locked"


@pytest.mark.parametrize("changer", ["reviewer", "reviewer2"])
def test_changes_from_either_seat_voids_round(w, changer):
    epoch = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "pass")  # round 1 verdict
    result = w.store.verdict(w.p(changer), KEY, False, "change please")
    assert result["state"] == "claimed"
    assert w.store._item(KEY)["round"] == 1  # no fresh submit yet
    # A fresh submit advances the round; the old verdict must not count.
    w.store.submit(w.p("l1main"), KEY, epoch)
    assert w.store._item(KEY)["round"] == 2
    one = w.store.verdict(w.p("reviewer"), KEY, True, "pass again")
    assert one["state"] == "in_review"  # one carried vote is not enough
    two = w.store.verdict(w.p("reviewer2"), KEY, True, "pass too")
    assert two["state"] == "done"
    assert _count(w, KEY, "review_verdict") == 3  # 1 voided + 2 fresh
    assert w.store.verify_integrity() == []


def test_fold_and_cache_agree_across_rounds(w):
    epoch = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "pass")
    w.store.verdict(w.p("reviewer2"), KEY, False, "changes")
    w.store.submit(w.p("l1main"), KEY, epoch)
    pass_review(w, KEY)
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []
    folded = keeper_events.fold(keeper_events.read_events(w.conn))
    item = w.store._item(KEY)
    for field in ("state", "epoch", "round", "reviewer"):
        assert folded[KEY][field] == item[field]
    assert folded[KEY]["round"] == 2
