"""Contract 10: decision-backed router verbs (waive, withdraw, reopen, answer, close refusals).

These use the fixture decisions file (capability ``decisions``): ``D-1`` is a
plain recorded decision, ``D-2`` closes ``demo:CLOSE-1``/``ANS-1``/``WD-1`` and
``D-3`` waives ``demo:HIGH-1``.  Those four keys are single-use per keeper.
"""
import hashlib

import pytest

from contract_world import (claim, err, evidence, make_plan, need, ok, show, submit,
                            to_in_review, verdict)


def _fixture_item(kp, ident, **fields):
    """Create ``demo:<ident>`` + a goal, or skip when a previous run used the key."""
    key = f"demo:{ident}"
    if kp.get(f"/v1/show?key={key}&fmt=json", "reader").status == 200:
        pytest.skip(f"fixture item {key} was already used on this keeper")
    item = {"project": "demo", "id": ident, "kind": "lane", "title": ident, "description": "d",
            "evidence_kinds": ["decision"], **fields}
    goal = {"project": "demo", "id": ident + "-GOAL", "kind": "goal", "title": ident + " goal",
            "depends_on": [ident]}
    ok(kp.post("create_items", "router", {"items": [item, goal]}), "Created")
    return key


def test_waive_second_family_on_a_high_tier_item(kp):
    need(kp, "decisions")
    key = _fixture_item(kp, "HIGH-1", review_tier="high")
    epoch = claim(kp, key)
    evidence(kp, key, epoch)
    for who in ("lead", "worker1", "reviewer1", "reader"):
        err(kp.post("waive_second_family", who, {"key": key, "decision_id": "D-3"}), 403, "forbidden")
    err(kp.post("waive_second_family", "router", {"key": key, "decision_id": "D-1"}),
        400, "evidence_unverified")                          # D-1 does not name the key
    err(kp.post("waive_second_family", "router", {"key": key, "decision_id": "D-404"}),
        400, "evidence_unverified")
    out = ok(kp.post("waive_second_family", "router", {"key": key, "decision_id": "D-3"}), "Waive")
    assert out == {"key": key, "state": "claimed", "review_waiver": "D-3"}
    assert show(kp, key)["review_waiver"] == "D-3"
    digest = ok(kp.get("/v1/digest", "router"), "Digest")
    assert {"key": key, "decision_id": "D-3", "state": "claimed"} in digest["waivers"]


def test_waiver_is_pointless_on_a_standard_item(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    claim(kp, plan.key)
    err(kp.post("waive_second_family", "router", {"key": plan.key, "decision_id": "D-1"}),
        400, "bad_state")


def test_withdraw_review_returns_an_item_to_the_claimant(kp):
    need(kp, "decisions")
    key = _fixture_item(kp, "WD-1")
    epoch = to_in_review(kp, key)
    for who in ("lead", "worker1", "reviewer1"):
        err(kp.post("withdraw_review", who, {"key": key, "decision_id": "D-2"}), 403, "forbidden")
    err(kp.post("withdraw_review", "router", {"key": key, "decision_id": "banana"}), 400, "validation")
    err(kp.post("withdraw_review", "router", {"key": key, "decision_id": "D-1"}),
        400, "evidence_unverified")                           # D-1 has no Closes: for the key
    out = ok(kp.post("withdraw_review", "router", {"key": key, "decision_id": "D-2"}), "Withdraw")
    assert out["key"] == key and out["state"] == "claimed" and out["epoch"] == epoch
    row = show(kp, key)
    assert row["state"] == "claimed" and row["claimed_by"] == "worker1" and row["lease_until"]
    err(kp.post("withdraw_review", "router", {"key": key, "decision_id": "D-2"}), 400, "bad_state")
    assert submit(kp, key, epoch)["round"] == 2               # the claimant may submit again


def test_reopen_returns_in_review_item_at_new_epoch(kp):
    need(kp, "decisions")
    key = _fixture_item(kp, "ROPEN-1")
    epoch = to_in_review(kp, key)
    for who in ("lead", "worker1", "reviewer1"):
        err(kp.post("reopen", who, {"key": key, "reason": "redo the work",
                                    "decision_id": "D-1"}), 403, "forbidden")
    err(kp.post("reopen", "router", {"key": key, "decision_id": "D-1"}), 400, "validation")   # no reason
    err(kp.post("reopen", "router", {"key": key, "reason": "   ",
                                     "decision_id": "D-1"}), 400, "validation")               # blank reason
    err(kp.post("reopen", "router", {"key": key, "reason": "redo the work",
                                     "decision_id": "D-999"}), 400, "evidence_unverified")
    out = ok(kp.post("reopen", "router", {"key": key, "reason": "redo the work",
                                          "decision_id": "D-1"}), "Reopen")
    assert out["key"] == key and out["state"] == "claimed" and out["epoch"] == epoch + 1
    row = show(kp, key)
    assert row["state"] == "claimed" and row["epoch"] == epoch + 1
    assert row["claimed_by"] == "worker1" and row["lease_until"]     # same claimant, fresh lease
    err(kp.post("reopen", "router", {"key": key, "reason": "again",
                                     "decision_id": "D-1"}), 400, "bad_state")


def test_answer_closes_a_question_on_a_decision(kp):
    need(kp, "decisions")
    key = _fixture_item(kp, "ANS-1", kind="question", evidence_kinds=[])
    for who in ("lead", "worker1", "reader"):
        err(kp.post("answer", who, {"key": key, "text": "yes", "decision_id": "D-2"}), 403, "forbidden")
    err(kp.post("answer", "router", {"key": key, "text": "  ", "decision_id": "D-2"}), 400, "validation")
    err(kp.post("answer", "router", {"key": key, "text": "yes", "decision_id": ""}), 400, "validation")
    err(kp.post("answer", "router", {"key": key, "text": "yes", "decision_id": "D-1"}),
        400, "evidence_unverified")
    lane = make_plan(kp).key
    err(kp.post("answer", "router", {"key": lane, "text": "yes", "decision_id": "D-2"}), 400, "validation")
    assert key in [r["key"] for r in ok(kp.get("/v1/needs_you?fmt=json", "reader"), "NeedsYou")]
    out = ok(kp.post("answer", "router", {"key": key, "text": "Go ahead.", "decision_id": "D-2"}), "Answer")
    assert out == {"key": key, "state": "done", "decision_id": "D-2"}
    assert key not in [r["key"] for r in ok(kp.get("/v1/needs_you?fmt=json", "reader"), "NeedsYou")]
    row = [r for r in ok(kp.get("/v1/answers?fmt=json", "reader"), "Answers") if r["key"] == key][0]
    assert row["answer"] == "Go ahead." and row["decision_id"] == "D-2"
    err(kp.post("answer", "router", {"key": key, "text": "again", "decision_id": "D-2"}), 409, "locked")


def test_close_on_decision_refusals(kp):
    need(kp, "decisions")
    plan = make_plan(kp, 2)
    standard, none_tier = plan.lanes[0], make_plan(kp, review_tier="none").key
    ref = "/nowhere/x.md@" + hashlib.sha256(b"x").hexdigest()
    for who in ("lead", "worker1", "reader"):
        err(kp.post("close_on_decision", who, {"key": standard, "batch_ref": ref}), 403, "forbidden")
    err(kp.post("close_on_decision", "router", {"key": standard, "batch_ref": ref}), 400, "bad_state")
    claim(kp, standard)
    err(kp.post("close_on_decision", "router", {"key": standard, "batch_ref": ref}), 400, "bad_state")
    err(kp.post("close_on_decision", "router", {"key": none_tier, "batch_ref": ref}), 400, "bad_state")  # open
    claim(kp, none_tier)
    err(kp.post("close_on_decision", "router", {"key": none_tier, "batch_ref": ref}),
        400, "evidence_missing")
    evidence(kp, none_tier, 1, did="D-1")
    for bad in ("not-a-ref", "/x@abc"):
        err(kp.post("close_on_decision", "router", {"key": none_tier, "batch_ref": bad}),
            400, "validation")
    err(kp.post("close_on_decision", "router", {"key": none_tier, "batch_ref": ref}),
        403, "forbidden")                                      # outside every batch root


def test_tier_none_items_take_no_verdicts(kp):
    need(kp, "decisions")
    plan = make_plan(kp, review_tier="none")
    to_in_review(kp, plan.key)
    err(verdict(kp, plan.key), 400, "bad_state")
    assert show(kp, plan.key)["state"] == "in_review"

