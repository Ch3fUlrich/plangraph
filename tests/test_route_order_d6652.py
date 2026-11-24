"""D-6652 (rule 3.66 (f)): Codex writes while it serves, Sonnet only while it is limited; two OpenCode seats review every head."""
import pytest

from plangraph.planner.legs import (DEFAULT_LEGS, REVIEW_ORDER, REVIEW_REQUIRED, SONNET_WRITER,
                                    SONNET_WRITER_HARD, SONNET_WRITERS)
from plangraph.planner.routing import plan_spawn

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}
REQUIRED_MODELS = {BY_ID[leg_id].model for leg_id in REVIEW_REQUIRED}


def reader(down=()):
    return lambda model, **_scope: {"state": "limited" if model in down else "available", "evidence": None}


def implement(card=None, down=()):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="implement",
                      leg_reader=reader(down), catalog_rows=())


def review(card=None, writers=("gpt",), down=()):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="review", writer_families=writers,
                      leg_reader=reader(down), catalog_rows=())


CODEX = {"cx-gpt-6.2-sol-medium", "omniroute/cx-gpt-6.2-sol-medium"}


def test_with_codex_serving_implement_never_returns_a_claude_leg():
    for card in ({}, {"hard": True}, {"risk_tags": ["secrets"]}):
        plan = implement(card)
        assert plan.first_leg.id == "cx-gpt-6.2-sol-medium", card
        assert not any(leg.claude_family or leg.family == "claude" for leg in plan.legs), card
        assert "codex_limited" not in plan.reasons
    # Hydrogen alone being limited does not admit Claude either.
    assert not any(leg.claude_family for leg in implement(down={"gemini-4-hydrogen-high"}).legs)


def test_with_codex_limited_implement_returns_sonnet_high_with_reason_codex_limited():
    plan = implement(down=CODEX)
    assert (plan.first_leg.id, plan.first_leg.harness, plan.first_leg.effort) == (
        "claude-sonnet-5-5-high", "claude", "high")
    assert "codex_limited" in plan.reasons
    assert SONNET_WRITER_HARD not in [leg.id for leg in plan.legs]


def test_the_run_record_carries_reason_codex_limited(tmp_path):
    import asyncio

    from plangraph.spawner.service import SpawnService
    for down, expected in ((CODEX, "codex_limited"), ((), None)):
        service = SpawnService(state=tmp_path, leg_reader=reader(down))
        record = asyncio.run(service.spawn(card={"estimate_tokens": 1000}, prompt="x", role="implement",
                                           dry_run=True))
        assert record.get("route_reason") == expected, down


def test_the_route_returns_to_codex_when_codex_serves_again(tmp_path):
    from plangraph.routing_support.leg_state import LegStateStore
    store = LegStateStore(tmp_path / "legs.sqlite")
    card = {"estimate_tokens": 1000}
    first = lambda now: plan_spawn(card, role="implement", leg_reader=store, now=now, catalog_rows=()).first_leg.id
    assert first(100) == "cx-gpt-6.2-sol-medium"
    store.record_provider_response("omniroute/cx-gpt-6.2-sol-medium", 429, "quota",
                                   {"Retry-After": "60"}, "fixture", now=100)
    assert first(110) == "claude-sonnet-5-5-high"
    assert first(100 + 61) == "cx-gpt-6.2-sol-medium"   # the limit expired: Codex again


def test_a_hard_card_gets_sonnet_xhigh_first_while_codex_is_limited():
    plan = implement({"hard": True}, down=CODEX)
    assert [(leg.id, leg.effort) for leg in plan.legs[:2]] == [
        (SONNET_WRITER_HARD, "xhigh"), (SONNET_WRITER, "high")]
    assert set(SONNET_WRITERS) <= {leg.id for leg in plan.legs}
    with pytest.raises(Exception, match="hard must be boolean"):
        implement({"hard": "yes"})


def test_review_route_seats_one_smart_leg_and_keeps_the_weak_pair_next():
    # Rule 3.66 (f10): one counted seat on Hydrogen; the two OpenCode seats (tools mode) follow as fallbacks.
    plan = review({"repository_review_required": True})
    assert [leg.id for leg in plan.review_seats] == ["agy-gemini-4-hydrogen-high"]
    assert [leg.id for leg in plan.legs[:5]] == ["agy-gemini-4-hydrogen-high", "agy-gemini-4-argon-low",
                                                 "agy-gemini-4-argon-medium", *REVIEW_REQUIRED]
    assert all(leg.harness == "opencode" and leg.tool_mode == "tools" for leg in plan.legs[3:5])
    assert list(REVIEW_ORDER[4:6]) == list(REVIEW_REQUIRED)
    assert not any(leg.claude_family for leg in plan.legs)


def test_review_needs_one_seat_on_a_standard_head_and_two_weak_seats_without_a_smart_leg():
    plan = review()
    assert len(plan.review_seats) == 1 and plan.error is None
    gemini = {BY_ID[i].model for i in ("agy-gemini-4-hydrogen-high",)} | {"gemini-4-argon-low", "gemini-4-argon-medium"}
    weak = review(down=gemini | {"claude-sonnet-5-5-high"})
    assert [leg.id for leg in weak.review_seats] == list(REVIEW_REQUIRED) and weak.error is None


def test_a_credential_tagged_diff_gets_the_claude_seat():
    for card in ({"risk_tags": ["secrets"]}, {"risk_tags": ["auth"]}, {"paths": ["src/auth/session.rs"]}):
        plan = review(card)
        assert plan.first_leg.id == "claude-sonnet-5-5-high", card
    assert "claude-sonnet-5-5-high" not in [leg.id for leg in review().legs]


def test_a_credential_diff_gets_one_sonnet_seat_and_the_weak_pair_stays_behind_it():
    for card in ({"risk_tags": ["secrets"]}, {"paths": ["src/auth/session.rs"]}):
        plan = review(card)
        assert [leg.id for leg in plan.review_seats] == ["claude-sonnet-5-5-high"] and plan.error is None
        assert set(REVIEW_REQUIRED) <= {leg.id for leg in plan.legs}
    # A Claude-written credential diff never gets the Claude seat.
    plan = review({"risk_tags": ["secrets"]}, writers=("claude",))
    assert [leg.id for leg in plan.review_seats] == ["agy-gemini-4-hydrogen-high"]


def test_the_claude_seat_serves_only_when_no_gemini_smart_leg_is_left():
    gemini = {"gemini-4-hydrogen-high", "gemini-4-argon-low", "gemini-4-argon-medium"}
    assert not any(leg.claude_family for leg in review(down={"gemini-4-hydrogen-high"}).legs)
    assert any(leg.id == "claude-sonnet-5-5-high" for leg in review(down=gemini).legs)
    # A deepseek-written diff fences one weak seat; Hydrogen still serves and Claude stays out.
    plan = review(writers=("deepseek",))
    assert plan.legs[0].id == "agy-gemini-4-hydrogen-high"
    assert not any(leg.claude_family for leg in plan.legs)
    # A claude-written diff never gets the Claude seat, even with every Gemini leg out.
    assert not any(leg.claude_family for leg in review(writers=("claude",), down=gemini).legs)
