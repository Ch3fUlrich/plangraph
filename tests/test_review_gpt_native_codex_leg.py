"""REVIEW-GPT-NATIVE-CODEX-LEG: the review route serves gpt-6.2-sol-medium first (rule 3.61)."""
from pathlib import Path

from plangraph.planner.legs import HARNESSES, REVIEW_ORDER
from plangraph.planner.routing import plan_spawn

CARD = {"estimate_tokens": 1000, "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
UP = lambda model, **kwargs: {"state": "available", "evidence": None}


def route(*writers):
    return [leg.id for leg in plan_spawn(CARD, role="review", writer_families=writers, leg_reader=UP).legs]


def test_review_order_names_gpt_first_then_mimo_deepseek_and_argon_late():
    assert REVIEW_ORDER[:5] == ("gpt-6.2-sol-medium", "mimo-v2.6-pro", "opencode-deepseek-v4.1-flash-free",
                                "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash")
    assert REVIEW_ORDER.index("agy-gemini-4-argon-low") > REVIEW_ORDER.index("opencode-deepseek-v4.1-flash-free")


def test_claude_written_diff_routes_gpt_first_in_codex():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",), leg_reader=UP)
    ids = [leg.id for leg in plan.legs]
    assert ids[:3] == ["gpt-6.2-sol-medium", "mimo-v2.6-pro", "opencode-deepseek-v4.1-flash-free"]
    assert plan.legs[0].harness == "codex" and plan.legs[0].family == "gpt"
    assert "codex" in HARNESSES
    assert ids.index("agy-gemini-4-argon-low") > ids.index("opencode-deepseek-v4.1-flash-free")


def test_gpt_written_diff_skips_the_gpt_leg():
    for writer in ("gpt", "openai"):
        ids = route(writer)
        assert "gpt-6.2-sol-medium" not in ids and ids[0] == "mimo-v2.6-pro"
