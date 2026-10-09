"""PG-WITHDRAW-REVIEW (D-867): router-only in_review -> claimed for decision-closable items.

Mutations killed: a non-router caller allowed, a decision that does not name the key accepted,
a code lane (commit evidence) withdrawn around its seats, the fold disagreeing with the cache,
tokens of the review left live, a withdraw from any state but in_review.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_in_review
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import verify_integrity


def _decisions(tmp_path, body):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n" + body, encoding="utf-8")
    return DecisionsAdapter(path)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("ISSUE", evidence_kinds=("decision",), done_criteria="upstream issue filed"),
        defn("RUN", evidence_kinds=("semaphore-task",), done_criteria="semaphore run green"),
        defn("CODE", evidence_kinds=("commit+ci",), done_criteria="merged; CI green; 2 seats"),
        defn("G", ["ISSUE", "RUN", "CODE"], kind="goal")])
    world.store.ctx = SimpleNamespace(decisions=_decisions(
        tmp_path, "| D-900 | closes | Closes: demo:ISSUE demo:RUN demo:CODE |\n"
                  "| D-901 | other | Closes: demo:OTHER |\n"))
    yield world
    world.close()


def _state(w, key):
    return w.conn.execute("SELECT state, reviewer, lease_until FROM items WHERE key=?",
                          (key,)).fetchone()


def test_router_withdraws_a_decision_item_and_the_fold_agrees(w):
    epoch = to_in_review(w, "demo:ISSUE")
    out = w.store.withdraw_review(w.p("router"), "demo:ISSUE", "D-900")
    assert out["state"] == "claimed" and out["epoch"] == epoch
    state, reviewer, lease = _state(w, "demo:ISSUE")
    assert state == "claimed" and reviewer is None and lease == out["lease_until"]
    assert verify_integrity(w.conn) == []
    # the claimant can now attach the decision evidence a decision close needs
    w.store.add_evidence(w.p("l1main"), "demo:ISSUE", epoch, "decision", {"id": "D-900"})
    ev = w.conn.execute("SELECT type, payload FROM events WHERE key='demo:ISSUE' "
                        "AND type='review_withdrawn'").fetchone()
    assert ev is not None and '"D-900"' in ev[1]


def test_semaphore_item_with_no_commit_criteria_is_withdrawable(w):
    to_in_review(w, "demo:RUN", kind="semaphore-task", ref={"template": 1, "task": 2})
    assert w.store.withdraw_review(w.p("router"), "demo:RUN", "D-900")["state"] == "claimed"


@pytest.mark.parametrize("who", ["l1main", "reviewer", "worker", "reader", "system"])
def test_only_the_router_may_withdraw(w, who):
    to_in_review(w, "demo:ISSUE")
    if who not in w.principals:
        pytest.skip(f"no {who} principal in the fixture")
    with pytest.raises(Refused) as exc:
        w.store.withdraw_review(w.p(who), "demo:ISSUE", "D-900")
    assert exc.value.code == "forbidden"
    assert _state(w, "demo:ISSUE")[0] == "in_review"


def test_decision_must_name_the_key(w):
    to_in_review(w, "demo:ISSUE")
    for did, code in (("D-901", "evidence_unverified"), ("D-999", "evidence_unverified"),
                      ("not-a-d", "validation")):
        with pytest.raises(Refused) as exc:
            w.store.withdraw_review(w.p("router"), "demo:ISSUE", did)
        assert exc.value.code == code
    assert _state(w, "demo:ISSUE")[0] == "in_review"


def test_code_lane_cannot_dodge_its_seats(w):
    to_in_review(w, "demo:CODE", kind="commit+ci", ref={"sha": "a" * 40})
    with pytest.raises(Refused) as exc:
        w.store.withdraw_review(w.p("router"), "demo:CODE", "D-900")
    assert exc.value.code == "forbidden"


def test_only_from_in_review(w):
    with pytest.raises(Refused) as exc:
        w.store.withdraw_review(w.p("router"), "demo:ISSUE", "D-900")       # open
    assert exc.value.code == "bad_state"


def test_withdraw_revokes_live_tokens_of_the_item():
    from plangraph.keeper import store_tokens, token_fold
    assert "review_withdrawn" in store_tokens._REVOKE_ALL
    assert store_tokens._REVOKE_ALL == token_fold.REVOKE_ALL
