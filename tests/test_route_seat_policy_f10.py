"""Rule 3.66 (f10): one counted smart seat; the two weak OpenCode seats only when no smart leg serves."""
import pytest

from plangraph.planner.legs import (review_route_pool, REVIEW_ARGON, REVIEW_ARGON_LOW, REVIEW_HYDROGEN,
                                    REVIEW_REQUIRED, REVIEW_SONNET)
from plangraph.planner.routing import plan_spawn

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")
BY_ID = {leg.id: leg for leg in review_route_pool()}
MODELS = {leg_id: BY_ID[leg_id].model for leg_id in (REVIEW_HYDROGEN, REVIEW_ARGON_LOW, REVIEW_ARGON)}
HYDROGEN, ARGONS = {MODELS[REVIEW_HYDROGEN]}, {MODELS[REVIEW_ARGON_LOW], MODELS[REVIEW_ARGON]}
GEMINI = HYDROGEN | ARGONS


def reader(down=()):
    return lambda model, **_scope: {"state": "limited" if model in down else "available", "evidence": None}


def review(writers, down=(), card=None):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="review", writer_families=writers,
                      leg_reader=reader(down), catalog_rows=())


def seats(plan):
    return [leg.id for leg in plan.review_seats]


def test_a_claude_head_gets_one_hydrogen_seat_while_hydrogen_serves():
    plan = review(("claude",))
    assert seats(plan) == [REVIEW_HYDROGEN] and plan.error is None
    assert plan.review_seats[0].tool_mode == "tools"
    assert plan.seat_coverage[0]["leg"] == REVIEW_HYDROGEN and plan.seat_coverage[0]["covers"]


def test_a_gpt_head_gets_one_hydrogen_seat():
    assert seats(review(("gpt",))) == [REVIEW_HYDROGEN]


def test_a_gemini_head_gets_one_sonnet_seat():
    plan = review(("gemini",))
    assert seats(plan) == [REVIEW_SONNET] and plan.error is None


def test_with_only_the_opencode_legs_serving_the_two_weak_seats_review():
    plan = review(("claude",), down=GEMINI)
    assert seats(plan) == list(REVIEW_REQUIRED) and plan.error is None
    assert REVIEW_SONNET not in [leg.id for leg in plan.legs]   # a Claude head never gets the Claude seat


def test_a_gemini_head_with_sonnet_unavailable_falls_to_the_weak_pair():
    # the Claude seat is not in the route when its quota is out; both weak seats seat the head.
    plan = review(("gemini",), down={BY_ID[REVIEW_SONNET].model, REVIEW_SONNET})
    assert seats(plan) == list(REVIEW_REQUIRED)


def test_the_route_chain_runs_smart_legs_before_the_weak_pair():
    ids = [leg.id for leg in review(("gpt",)).legs]
    assert ids[0] == REVIEW_HYDROGEN
    assert max(ids.index(i) for i in (REVIEW_HYDROGEN, REVIEW_ARGON_LOW, REVIEW_ARGON)) < min(
        ids.index(i) for i in REVIEW_REQUIRED)
    # a seat run that ends without a verdict falls to the next smart leg: hydrogen out leaves argon first
    ids = [leg.id for leg in review(("gpt",), down=HYDROGEN).legs]
    assert ids[0] == REVIEW_ARGON_LOW and ids.index(REVIEW_ARGON_LOW) < ids.index(REVIEW_REQUIRED[0])


def test_hydrogen_out_gives_one_argon_seat_not_the_weak_pair():
    assert seats(review(("claude",), down=HYDROGEN)) == [REVIEW_ARGON_LOW]


def test_a_credential_diff_on_a_non_claude_head_gets_sonnet_and_keeps_the_weak_pair_for_the_keeper():
    # The keeper close needs two non-Anthropic families on a credential card, so the pair stays seated.
    assert seats(review(("gpt",), card={"risk_tags": ["secrets"]})) == [REVIEW_SONNET, *REVIEW_REQUIRED]


def test_the_lone_smart_seat_slot_holds_the_next_smart_legs_before_the_weak_pair():
    """The production seat run restricts re-routing to the slot: hydrogen's slot must reach argon first."""
    from plangraph.spawner.seat_artifact import reserve_seat_families
    plan = review(("gpt",))
    spares, owners = reserve_seat_families(plan, ("gpt",))
    ids = [leg.id for leg in spares[0]]
    assert ids[:2] == [REVIEW_ARGON_LOW, REVIEW_ARGON]
    assert ids.index(REVIEW_ARGON) < ids.index(REVIEW_REQUIRED[0])
    assert REVIEW_HYDROGEN not in ids
