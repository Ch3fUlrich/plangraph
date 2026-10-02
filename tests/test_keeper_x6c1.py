"""X6c1 — liveness bug in ``_verdict_changes`` and waiver clearing on cancel.

Two review findings:

1. ``_verdict_changes`` counted ``changes_requested`` events over the item's
   whole lifetime, so one change request in an earlier claim made the *first*
   change request of every later claim drop the item straight to ``open`` and
   file a false ``changes_requested_twice`` finding.  A ``changes_requested``
   event now carries ``epoch`` and ``round``, and "twice in a row" is counted
   inside the current epoch only: consecutive changes with no passing verdict
   between them.
2. ``cancel`` abandons the work, so its waiver must not outlive it: both
   ``_cancel``'s UPDATE and the ``cancelled`` fold branch clear
   ``review_waiver``, and ``_waiver_active`` asks the same truthiness question
   the digest does.
"""
import json
from types import SimpleNamespace

import pytest

from keeper_util import World, seed, to_claimed, to_in_review
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
DECISION = "D-217"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])], review_tier="high")
    # X6c3: a waiver needs a D-row that names the item (`Waives: demo:A`).
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Project | Decision |\n|---|---|---|\n"
                    "| D-217 | second-family waiver | Waives: demo:A |\n",
                    encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    yield world
    world.close()


def _findings(world):
    return [row[0] for row in world.conn.execute(
        "SELECT code FROM audit_findings WHERE key=?", (KEY,)).fetchall()]


def _payloads(world, type):
    return [json.loads(row["payload"] or "{}") for row in world.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type=?",
        (KEY, type)).fetchall()]


def _waive(world):
    world.store.waive_second_family(world.p("router"), KEY, DECISION)


# ------------------------------------------------ item 1: epoch-scoped changes

def test_changes_requested_payload_carries_epoch_and_round(w):
    epoch = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "one")
    payload = _payloads(w, "changes_requested")[-1]
    assert payload["epoch"] == epoch
    assert payload["round"] == 1


def test_change_request_in_later_epoch_stays_claimed(w):
    """The liveness bug: a stale epoch-1 change must not reopen epoch 2."""
    epoch1 = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "epoch one")
    assert w.store._item(KEY)["state"] == "claimed"

    w.store.release(w.p("l1main"), KEY, epoch1)
    epoch2 = to_in_review(w, KEY)
    assert epoch2 != epoch1

    result = w.store.verdict(w.p("reviewer"), KEY, False, "epoch two, first")
    assert result["state"] == "claimed"
    item = w.store._item(KEY)
    assert item["state"] == "claimed"
    assert item["epoch"] == epoch2
    assert item["lease_until"] is not None
    assert _findings(w) == []
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_two_changes_in_a_row_within_later_epoch_reopens(w):
    """The reopen still fires twice in a row, just counted per epoch."""
    epoch1 = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "epoch one")
    w.store.release(w.p("l1main"), KEY, epoch1)
    epoch2 = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "epoch two, first")
    w.store.submit(w.p("l1main"), KEY, epoch2)
    result = w.store.verdict(w.p("reviewer"), KEY, False, "epoch two, second")

    assert result["state"] == "open"
    assert _findings(w) == ["changes_requested_twice"]
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_two_changes_in_a_row_within_epoch_reopens(w):
    """The rule that must survive the fix: second change of an epoch reopens."""
    epoch = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "one")
    w.store.submit(w.p("l1main"), KEY, epoch)
    result = w.store.verdict(w.p("reviewer"), KEY, False, "two")

    assert result["state"] == "open"
    assert w.store._item(KEY)["state"] == "open"
    assert _findings(w) == ["changes_requested_twice"]
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_passing_verdict_between_changes_resets_the_streak(w):
    """'in a row' means consecutive: a pass in between restarts the count."""
    epoch = to_claimed(w, KEY)
    w.store.add_evidence(w.p("l1main"), KEY, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), KEY, epoch)
    assert w.store.verdict(w.p("reviewer2"), KEY, True, "pass")["state"] == (
        "in_review")
    w.store.verdict(w.p("reviewer"), KEY, False, "changes after a pass")
    w.store.submit(w.p("l1main"), KEY, epoch)
    result = w.store.verdict(w.p("reviewer"), KEY, False, "second since pass")

    assert result["state"] == "open"
    assert _findings(w) == ["changes_requested_twice"]
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_fold_agrees_after_cross_epoch_change_requests(w):
    """fold/verify_integrity must still match once events carry an epoch."""
    epoch1 = to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, False, "one")
    w.store.release(w.p("l1main"), KEY, epoch1)
    to_in_review(w, KEY, who="l1main")
    w.store.verdict(w.p("reviewer"), KEY, False, "two")

    folded = keeper_events.fold(keeper_events.read_events(w.conn))[KEY]
    item = w.store._item(KEY)
    assert folded["state"] == item["state"] == "claimed"
    assert folded["epoch"] == item["epoch"]
    assert folded["lease_until"] == item["lease_until"]
    assert w.store.verify_integrity() == []


# ------------------------------------------------------- item 4: cancel waiver

def test_cancel_clears_waiver_in_table_and_fold(w):
    to_in_review(w, KEY)
    _waive(w)
    assert w.store._item(KEY)["review_waiver"] == DECISION

    w.store.cancel(w.p("router"), KEY, "abandoned")
    item = w.store._item(KEY)
    assert item["state"] == "cancelled"
    assert item["review_waiver"] is None
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_cancelled_item_is_no_longer_waived(w):
    to_in_review(w, KEY)
    _waive(w)
    assert w.store._waiver_active(w.store._item(KEY)) is True
    w.store.cancel(w.p("router"), KEY, "abandoned")
    assert w.store._waiver_active(w.store._item(KEY)) is False


def test_waiver_active_matches_digest_truthiness(w):
    """``_waiver_active`` and ``digest`` ask the same question of the column."""
    from plangraph.keeper.store_review import ReviewMixin

    for value in (None, "", DECISION):
        item = {"review_waiver": value}
        active = ReviewMixin._waiver_active(object(), item)
        listed = bool(item["review_waiver"])
        assert active == listed, value


def test_digest_still_lists_pending_waivers(w):
    to_in_review(w, KEY)
    _waive(w)
    digest = w.store.digest(w.p("router"))
    assert {"key": KEY, "decision_id": DECISION, "state": "in_review"} in (
        digest["waivers"])

    w.store.cancel(w.p("router"), KEY, "abandoned")
    assert w.store.digest(w.p("router"))["waivers"] == []


def test_cancel_of_claimed_item_clears_waiver(w):
    to_claimed(w, KEY)
    _waive(w)
    assert w.store._item(KEY)["review_waiver"] == DECISION
    result = w.store.cancel(w.p("router"), KEY, "stop")
    assert result["state"] == "cancelled"
    assert w.store._item(KEY)["review_waiver"] is None
    assert w.store.verify_integrity() == []


def test_cancel_needs_no_waiver_and_refuses_after_done(w):
    to_in_review(w, KEY)
    with pytest.raises(Refused):
        w.store.cancel(w.p("workerA"), KEY, "no role")
