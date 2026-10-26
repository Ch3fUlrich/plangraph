"""D-2983/D-3005 operator order and D-2744 removal through spawn planning."""
import sys

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn

CARD = {"estimate_tokens": 1000}
REVIEW = [
    "cx-gpt-6-luna", "qodercli-qwen3.8-flash", "opencode-glm-5.3-flash-free",
    "agy-gemini-3.8-flash", "mimo-v2.6-flash",
]


def test_review_order_claude_writer():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    assert [leg.id for leg in plan.legs] == REVIEW


def test_review_order_gpt_writer():
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",))
    assert [leg.id for leg in plan.legs] == REVIEW[1:]
    assert any(s["leg"] == REVIEW[0] and s["reason"] == "excluded_family"
               for s in plan.skipped)


def test_implement_order_operator():
    plan = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in plan.legs] == [
        "cx-gpt-6.1-sol", "mimo-v2.6-pro", "agy-gemini-3.8-flash",
        "openrouter-free", "opencode-glm-5.3-flash-free", "mimo-v2.6-flash",
    ]


def test_no_deepseek_legs_or_models():
    assert all("deepseek" not in value.lower()
               for leg in DEFAULT_LEGS for value in (leg.id, leg.model))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
