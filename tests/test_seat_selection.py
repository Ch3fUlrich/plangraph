"""E3b seat-family selection (D-2913): the code-owned seat order and its legs.

Part 1 tests. The pick/probe/verify rows land with their scope items; this
file grows with them. Everything is hermetic: fake probe, fake leg_state.
"""
import dataclasses

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, SEAT_ORDER
from plangraph.planner.routing import validate_model

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
