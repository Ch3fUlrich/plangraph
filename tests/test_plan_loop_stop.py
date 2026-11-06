"""Pure stop-rule checks, without seats or filesystem material."""
from copy import deepcopy

import pytest

from tools.plan_loop_stop import evaluate


def state():
    manifest = dict(text_sha="a", history=[dict(sha="a", material=True)],
                    plan_chars_r1=100, plan_chars=100,
                    report=dict(sha="a", ok=True, claims=["C1"]),
                    claims={"C1": dict(state="verified")})
    seats = [seat(family=f) for f in ("deepseek", "qwen", "glm", "gpt")]
    seats[0].update(kind="scope-cut")
    return [], manifest, seats, dict(slug="sample", accept_families=4)


def seat(**changes):
    return dict(round=1, kind="discovery", family="gpt", run_id="run",
                sha="a", witnessed=True, witness_kind="served-family",
                status="completed", verdict="ACCEPT", counts={},
                new_classes=[]) | changes


def test_four_families_and_alias():
    args = state()
    assert evaluate(*args).state == "CLEAN"
    args[2][-1]["family"] = "openai"
    args[2].append(seat(family="gpt"))
    assert len(evaluate(*args).families) == 4
    args[2].pop(1)
    assert evaluate(*args).state == "OPEN"


@pytest.mark.parametrize("change", ["open", "unclosed", "latest", "scope", "report", "size"])
def test_each_condition(change):
    ledger, manifest, seats, policy = state()
    if change in {"open", "unclosed"}:
        ledger.append(dict(severity="major", disposition="open" if change == "open" else "accepted",
                           closed_by="", unverified=True))
    elif change == "latest":
        seats[-1]["new_classes"] = ["new"]
    elif change == "scope":
        seats[0]["kind"] = "discovery"
    elif change == "report":
        manifest["report"]["ok"] = False
    else:
        manifest["plan_chars"] = 116
    assert evaluate(ledger, manifest, seats, policy).state == "OPEN"


def test_witness_and_operator_chat():
    args = state()
    last = args[2][-1]
    last.update(witnessed=False)
    assert "unwitnessed_accepts=openai" in str(evaluate(*args))
    last.update(witness_kind="operator-chat", witnessed=True)
    assert evaluate(*args).state == "OPEN"
    last.update(decision="D-1234", decision_by="reviewer")
    assert evaluate(*args).state == "CLEAN"
    last["decision_by"] = "planner"
    assert evaluate(*args).state == "OPEN"


def test_minor_chain_requires_each_exact_diff():
    args = state()
    manifest, seats = args[1:3]
    manifest.update(text_sha="c")
    manifest["history"] += [dict(sha="b", from_sha="a", material=False),
                            dict(sha="c", from_sha="b", material=False)]
    assert evaluate(*args).next == "diff"
    seats.append(seat(kind="diff", sha="b", from_sha="a"))
    seats.append(seat(kind="diff", sha="c", from_sha="b", witnessed=False))
    assert evaluate(*args).next == "diff"
    seats.append(seat(kind="diff", sha="c", from_sha="b"))
    assert evaluate(*args).state == "CLEAN"
    seats[-1]["verdict"] = "REQUEST_CHANGES"
    assert not evaluate(*args).families


def test_material_change_lapses():
    args = state()
    args[1].update(text_sha="b")
    args[1]["history"].append(dict(sha="b", material=True))
    assert not evaluate(*args).families


def test_rounds_cap_is_sticky():
    args = state()
    args[2][:] = [seat(round=n, kind=k) for n, k in enumerate(
        ["r0", "confirm", "discovery", "stacked", "scope-cut", "confirm", "stacked", "stacked"])]
    result = evaluate(*args)
    assert result.rounds == 6
    assert str(result) == "BLOCKED plan:sample | review-loop-capped | options: raise cap by D-row, *replan"
    args[1]["capped"] = True
    args[2][:] = state()[2]
    assert evaluate(*args).state == "BLOCKED"


def test_stall_and_waiver():
    args = state()
    args[2][:] = [seat(status="failed", round=1) for _ in range(3)]
    assert "review-loop-stalled" in str(evaluate(*args))
    args[1]["waived"] = "D-1234"
    assert evaluate(*args).state == "WAIVED"
    args[3]["tags"] = ["auth"]
    assert evaluate(*args).state == "BLOCKED"


@pytest.mark.parametrize("claim,clean", [
    ({"state": "false"}, False),
    ({"state": "unverifiable"}, False),
    ({"state": "not-load-bearing", "evidence": "reference"}, True),
    ({"state": "verified", "evidence": "reference"}, True),
    ({"state": "open-question"}, True),
])
def test_claim_settlement(claim, clean):
    args = state()
    args[1]["claims"]["C1"] = claim
    assert (evaluate(*args).state == "CLEAN") == clean
    if claim["state"] == "open-question":
        args[3]["tier"] = "high"
        assert evaluate(*args).state == "OPEN"


def test_pure_and_false_claim_later_freeze():
    args = state()
    args[1]["history"].insert(0, dict(sha="old", material=True))
    args[1]["report"]["sha"] = "old"
    args[1]["history"][-1]["material"] = False
    args[1]["history"][-1]["from_sha"] = "old"
    args[2].append(seat(kind="diff", from_sha="old"))
    args[1]["claims"]["C1"] = dict(state="false", sha_fixed="a")
    before = deepcopy(args)
    assert evaluate(*args).state == "CLEAN"
    assert args == before


def test_report_material_binding_and_no_claims():
    args = state()
    args[1]["report"]["claims"] = []
    assert evaluate(*args).next == "r0"
    args[1]["report"].update(claims=["C1"], sha="old")
    assert evaluate(*args).state == "OPEN"


def test_round_counts_multi_seat_and_failures():
    args = state()
    args[2][:] = [seat(round=1), seat(round=1, family="glm"),
                  seat(round=2, kind="confirm"), seat(round=3, kind="diff"),
                  seat(round=4, kind="stacked", status="failed"),
                  seat(round=5, kind="confirm")]
    assert evaluate(*args).rounds == 2


def test_growth_cite_and_stacked_scope():
    args = state()
    args[2][0].update(kind="stacked", lens="scope-cut")
    args[1]["plan_chars"] = 200
    args[1]["history"][0]["growth_cite"] = "R1.s.1"
    args[0].append(dict(id="R1.s.1", claim="Required explanation", severity="minor",
                        disposition="deferred"))
    assert evaluate(*args).state == "CLEAN"
    args[3]["tier"] = "high"
    assert evaluate(*args).state == "OPEN"
