"""REVIEW-GPT-NATIVE-CODEX-LEG: the review route keeps gpt-6.2-sol-medium in Codex after Hydrogen (D-6550)."""
from pathlib import Path

from plangraph.planner.legs import HARNESSES, REVIEW_ORDER
from plangraph.planner.routing import plan_spawn

CARD = {"estimate_tokens": 1000, "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
UP = lambda model, **kwargs: {"state": "available", "evidence": None}


def route(*writers):
    return [leg.id for leg in plan_spawn(CARD, role="review", writer_families=writers, leg_reader=UP).legs]


def test_review_order_names_gpt_first_then_mimo_deepseek_and_argon_late():
    assert REVIEW_ORDER[:6] == ("agy-gemini-4-hydrogen-high", "gpt-6.2-sol-medium", "mimo-v2.6-pro", "opencode-deepseek-v4.1-flash-free",
                                "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash")
    assert REVIEW_ORDER[-2:] == ("agy-gemini-4-argon-low", "agy-gemini-4-argon-medium")


def test_claude_written_diff_routes_gpt_first_in_codex():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",), leg_reader=UP)
    ids = [leg.id for leg in plan.legs]
    assert ids[:3] == ["agy-gemini-4-hydrogen-high", "gpt-6.2-sol-medium", "mimo-v2.6-pro"]
    assert plan.legs[1].harness == "codex" and plan.legs[1].family == "gpt"
    assert "codex" in HARNESSES
    assert ids == ["agy-gemini-4-hydrogen-high", "gpt-6.2-sol-medium", "mimo-v2.6-pro", "opencode-deepseek-v4.1-flash-free",
                   "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash", "agy-gemini-4-neon-medium",
                   "mimo-v2.6-flash", "cx-gpt-6-luna", "agy-gpt-oss-120b",
                   "agy-gemini-4-argon-low", "agy-gemini-4-argon-medium"]   # argon last


def test_gpt_written_diff_skips_the_gpt_leg():
    for writer in ("gpt", "openai"):
        ids = route(writer)
        assert "gpt-6.2-sol-medium" not in ids and ids[0] == "agy-gemini-4-hydrogen-high"


def test_argon_is_the_end_of_the_whole_eligible_route():
    ids = route("claude")
    assert ids[-2:] == ["agy-gemini-4-argon-low", "agy-gemini-4-argon-medium"]
    assert ids.index("cx-gpt-6-luna") < ids.index("agy-gemini-4-argon-low")


def test_agy_account_limit_keeps_native_codex_review_leg_available(tmp_path):
    from plangraph.planner.legs import REVIEW_GPT, REVIEW_NEON, REVIEW_SONNET
    from plangraph.planner.route_policy import RoutePolicy
    from plangraph.routing_support.leg_state import LegStateStore

    store = LegStateStore(tmp_path / "legs.sqlite")
    store.record_harness_limit("agy/gemini-4-argon-low", "Individual quota reached. Resets in 2h.",
                               "fixture", now=100, account="A")

    def route(account):
        return plan_spawn(dict(CARD, account=account), role="review",
                          writer_families=("deepseek",), leg_reader=store, now=101,
                          policy=RoutePolicy(), catalog_rows=(), installed=lambda _: True)

    limited = route("A")
    codex = limited.first_leg
    assert (codex.id, codex.harness, codex.model) == (REVIEW_GPT, "codex", "gpt-6.2-sol-medium")
    assert REVIEW_SONNET in {leg.id for leg in limited.legs}
    evidence = next(row["evidence"] for row in limited.skipped if row["leg"] == REVIEW_NEON)
    assert evidence["quota_scope"] == "account"
    assert evidence["harness"] == "agy" and evidence["account"] == "A"
    assert REVIEW_NEON in {leg.id for leg in route("B").legs}
