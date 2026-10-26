"""E3b seat-family selection (D-2913): the code-owned seat order and its legs.

Part 1 tests. The pick/probe/verify rows land with their scope items; this
file grows with them. Everything is hermetic: fake probe, fake leg_state.
"""
import dataclasses

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, SEAT_ORDER
from plangraph.planner.routing import plan_spawn, validate_model
from plangraph.review.family import FamilyError, check_cross_family, model_family

# The six console-openrouter / opencode free review legs of the slice, with
# the exact model ids the host listing showed (design E3b "catalog legs").
NEW_REVIEW_LEGS = {
    "opencode-mimo-v2.6-flash-free": "opencode/mimo-v2.6-flash-free",
    "or-nemotron-3-ultra-free": "console-openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "or-gemma-4-31b-free": "console-openrouter/google/gemma-4-31b-it:free",
    "or-inkling-free": "console-openrouter/thinkingmachines/inkling:free",
    "or-qwen3.8-27b-free": "console-openrouter/qwen/qwen3.8-27b:free",
    "or-north-mini-free": "console-openrouter/cohere/north-mini-code:free",
}


def test_new_review_legs_shape():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id, model in NEW_REVIEW_LEGS.items():
        leg = by_id.get(leg_id)
        assert leg is not None, leg_id
        assert leg.model == model
        assert leg.roles == ("review",), leg_id
        assert leg.tier == "free", leg_id
        assert leg.trains_on_data is True, leg_id
        assert leg.harness == "opencode", leg_id


def test_new_legs_pass_validate_model():
    for leg_id, model in NEW_REVIEW_LEGS.items():
        assert validate_model(model) == model, leg_id


def test_seat_order_is_the_code_owned_order():
    assert SEAT_ORDER == (
        "qodercli-qwen3.8-flash",
        "opencode-mimo-v2.6-flash-free",
        "or-nemotron-3-ultra-free",
        "or-gemma-4-31b-free",
        "or-inkling-free",
        "or-qwen3.8-27b-free",
        "or-north-mini-free",
        "agy-sonnet-5.5",
        "opencode-glm-5.3-flash-free",
    )
    ids = {leg.id for leg in DEFAULT_LEGS}
    assert all(leg_id in ids for leg_id in SEAT_ORDER)
    assert SEAT_ORDER[-1] == "opencode-glm-5.3-flash-free"  # glm last (D-2913)


def test_deepseek_is_never_a_seat():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    assert "review" not in by_id["opencode-deepseek-v4.1-flash-free"].roles
    assert all("deepseek" not in leg_id for leg_id in SEAT_ORDER)


# --- E3b item 3: plan_spawn picks the review seats by itself (D-2913) ---

def _card(risk=None):
    card = {"estimate_tokens": 2000}
    if risk is not None:
        card["risk"] = risk
    return card


def _leg_state(available=None, limited=()):
    """Fake leg_state reader; with `available`, everything else is limited."""
    available = None if available is None else set(available)

    def reader(model, now=None, account=None, combo=None):
        if available is not None and model not in available:
            state = "limited"
        else:
            state = "limited" if model in limited else "ok"
        return {"state": state, "evidence": "fake"}

    return reader


def _seat_ids(plan):
    return [leg.id for leg in plan.review_seats]


def test_seat_order_beats_tier_position():
    # Every family ahead of anthropic and zhipu is a writer: the code-owned
    # order seats sonnet (subscription tier) before glm (free tier).
    writers = ("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere")
    plan = plan_spawn(_card("high"), role="review", writer_families=writers,
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["agy-sonnet-5.5", "opencode-glm-5.3-flash-free"]
    assert plan.error is None
    # negative control: block anthropic too and only glm is left, with the
    # existing error text kept when fewer seats than n are found
    plan = plan_spawn(_card("high"), role="review", writer_families=writers + ("anthropic",),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]
    assert plan.error == "two distinct review families unavailable"


def test_review_seats_by_writer_family_and_risk():
    # writers xiaomi + anthropic, risk high: qwen then nemotron-ultra
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash", "or-nemotron-3-ultra-free"]
    # writer alibaba: mimo-flash-free then nemotron-ultra
    plan = plan_spawn(_card("high"), role="review", writer_families=("alibaba",),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-mimo-v2.6-flash-free", "or-nemotron-3-ultra-free"]
    # risk standard: one seat
    plan = plan_spawn(_card(), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash"]
    assert plan.error is None


def test_writer_family_spellings_pick_the_same_seats():
    picks = {}
    for spelling in ("qwen", "alibaba", "mimo", "xiaomi"):
        plan = plan_spawn(_card("high"), role="review", writer_families=(spelling,),
                          leg_reader=_leg_state(), legs=DEFAULT_LEGS)
        picks[spelling] = _seat_ids(plan)
    assert picks["qwen"] == picks["alibaba"]
    assert picks["mimo"] == picks["xiaomi"]
    # positive control on the real pick, not just equality
    assert picks["alibaba"] == ["opencode-mimo-v2.6-flash-free", "or-nemotron-3-ultra-free"]
    # negative control: an unknown writer family is refused, never guessed
    with pytest.raises(FamilyError):
        plan_spawn(_card(), role="review", writer_families=("kling",),
                   leg_reader=_leg_state(), legs=DEFAULT_LEGS)


def test_glm_is_picked_only_when_every_earlier_family_is_blocked():
    writers = ("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere")
    # anthropic's leg is unavailable; every other family ahead of glm is a writer
    plan = plan_spawn(_card(), role="review", writer_families=writers,
                      leg_reader=_leg_state(limited=("sonnet-5.5", "agy/sonnet-5.5")),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]
    # negative control: one earlier family free (cohere) takes the seat first
    plan = plan_spawn(_card(), role="review", writer_families=writers[:-1],
                      leg_reader=_leg_state(limited=("sonnet-5.5", "agy/sonnet-5.5")),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["or-north-mini-free"]


def test_deepseek_is_refused_even_when_a_config_lists_it():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = tuple(
        dataclasses.replace(by_id["or-nemotron-3-ultra-free"],
                            model="opencode/deepseek-v4.1-flash-free", family="deepseek")
        if leg.id == "or-nemotron-3-ultra-free" else leg for leg in DEFAULT_LEGS)
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=legs)
    seats = _seat_ids(plan)
    assert seats == ["qodercli-qwen3.8-flash", "or-gemma-4-31b-free"]
    assert any(s["leg"] == "or-nemotron-3-ultra-free" and s["reason"] == "deepseek_seat"
               for s in plan.skipped)


def test_family_table_maps_the_new_legs():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    expected = {
        "or-gemma-4-31b-free": "google",
        "or-inkling-free": "thinkingmachines",
        "or-north-mini-free": "cohere",
        "or-qwen3.8-27b-free": "alibaba",
    }
    for leg_id, family in expected.items():
        assert model_family(by_id[leg_id].model) == family, leg_id
    # negative control: an unknown model id has no family (refused, not guessed)
    assert model_family("totally/unknown-model") is None


def test_check_cross_family_agrees_with_plan_spawn():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id, model in NEW_REVIEW_LEGS.items():
        family = model_family(model)
        # cross-family: the keeper's table and the spawner both seat this leg
        assert check_cross_family("openai", model) == family
        plan = plan_spawn(_card(), role="review", writer_families=("openai",),
                          leg_reader=_leg_state(available=(model,)), legs=DEFAULT_LEGS)
        assert _seat_ids(plan) == [leg_id], leg_id
        # negative control: the writer's own family is refused by both
        with pytest.raises(FamilyError):
            check_cross_family(family, model)
        plan = plan_spawn(_card(), role="review", writer_families=(family,),
                          leg_reader=_leg_state(), legs=DEFAULT_LEGS)
        assert leg_id not in _seat_ids(plan), leg_id
