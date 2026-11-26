"""D-6652 (rule 3.66 (f)): Codex writes while it serves, Sonnet only while it is limited; two OpenCode seats review every head."""
import pytest

from plangraph.planner.legs import (DEFAULT_LEGS, REVIEW_ORDER, REVIEW_REQUIRED, SONNET_WRITER,
                                    SONNET_WRITER_HARD, SONNET_WRITERS, explicit_only_skips)
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.leg_state import LegStateStore

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}
REQUIRED_MODELS = {BY_ID[leg_id].model for leg_id in REVIEW_REQUIRED}
AGY_SONNET = "agy-claude-sonnet-5-5-high"
SUBSCRIPTION_SONNET = "claude-sonnet-5-5-high"


def reader(down=()):
    return lambda model, **_scope: {"state": "limited" if model in down else "available", "evidence": None}


def implement(card=None, down=()):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="implement",
                      leg_reader=reader(down), catalog_rows=())


def review(card=None, writers=("gpt",), down=()):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="review", writer_families=writers,
                      leg_reader=reader(down), catalog_rows=())


CODEX = {"cx-gpt-6.2-sol-medium", "omniroute/cx-gpt-6.2-sol-medium"}


def test_agy_quota_states_share_the_gemini_pool(tmp_path):
    store = LegStateStore(tmp_path / "legs.sqlite")
    store.record_harness_limit("agy/gemini-4-argon-low", "HTTP 429: quota reached", "fixture", now=100)
    gemini = {"agy-gemini-4-argon-low", "agy-gemini-4-argon-medium",
              "agy-gemini-4-hydrogen-high", "agy-gemini-4-hydrogen-medium"}
    for role in ("review", "research"):
        plan = plan_spawn({"estimate_tokens": 1000}, role=role, writer_families=("gpt",),
                          leg_reader=store, now=101, catalog_rows=())
        assert plan.agy_quota_states["agy:gemini-4-argon"] == plan.agy_quota_states["agy:gemini-4-hydrogen"]
        assert plan.agy_quota_states["agy:gemini-4-argon"]["state"] == "limited"
        assert {entry["leg"] for entry in plan.skipped if entry["reason"] == "limited"} >= gemini
        assert gemini.isdisjoint(leg.id for leg in plan.legs)


def test_implement_route_unchanged_gpt_first():
    for card in ({}, {"hard": True}, {"risk_tags": ["secrets"]}):
        plan = implement(card)
        assert (plan.first_leg.id, plan.first_leg.harness) == ("cx-gpt-6.2-sol-medium", "codex"), card
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


def test_review_route_returns_the_two_required_opencode_seats_with_tools():
    plan = review({"repository_review_required": True})
    assert [leg.id for leg in plan.review_seats] == list(REVIEW_REQUIRED)
    assert all(leg.harness == "opencode" and leg.tool_mode == "tools" for leg in plan.review_seats)
    assert [leg.id for leg in plan.legs[:2]] == list(REVIEW_REQUIRED) == list(REVIEW_ORDER[:2])
    assert not any(leg.claude_family for leg in plan.legs)


def test_review_needs_two_seats_on_a_standard_head_too():
    plan = review()
    assert len(plan.review_seats) == 2 and plan.error is None


def test_review_route_agy_sonnet_before_subscription():
    plan = review({"risk_tags": ["auth"]})
    index = REVIEW_ORDER.index(SUBSCRIPTION_SONNET)
    assert REVIEW_ORDER[index - 1] == AGY_SONNET
    assert (plan.first_leg.id, plan.first_leg.harness) == (AGY_SONNET, "agy")
    assert plan.review_seats[0] == plan.first_leg
    assert plan.seat_coverage[0] == {"leg": AGY_SONNET, "covers": ["openai"]}
    assert SUBSCRIPTION_SONNET not in {leg.id for leg in plan.legs}
    assert AGY_SONNET not in {row["leg"] for row in explicit_only_skips("review")}


def test_review_route_subscription_when_agy_sonnet_limited(tmp_path):
    store = LegStateStore(tmp_path / "legs.sqlite")
    store.record_provider_response("agy:claude-sonnet-5-5", 429, "quota",
                                   {"Retry-After": "60"}, "fixture", now=100)
    card = {"estimate_tokens": 1000, "risk_tags": ["auth"]}

    def route(now):
        return plan_spawn(card, role="review", writer_families=("gpt",),
                          leg_reader=store, now=now, catalog_rows=(), installed=lambda _: True)

    limited = route(101)
    assert (limited.first_leg.id, limited.first_leg.harness) == (SUBSCRIPTION_SONNET, "claude")
    assert limited.review_seats[0] == limited.first_leg
    assert limited.seat_coverage[0]["leg"] == SUBSCRIPTION_SONNET
    blocked = next(row for row in limited.skipped if row["leg"] == AGY_SONNET)
    assert blocked["evidence"]["status"] == 429
    assert route(160).first_leg.id == AGY_SONNET


def test_review_route_subscription_when_agy_client_missing():
    plan = plan_spawn({"estimate_tokens": 1000, "risk_tags": ["auth"]}, role="review",
                      writer_families=("gpt",), leg_reader=reader(), catalog_rows=(),
                      installed=lambda name: name != "agy")
    assert (plan.first_leg.id, plan.first_leg.harness) == (SUBSCRIPTION_SONNET, "claude")
    assert any(row["leg"] == AGY_SONNET and row["reason"] == "client_missing" for row in plan.skipped)


def test_review_route_does_not_use_subscription_for_a_paused_agy_client():
    plan = plan_spawn({"estimate_tokens": 1000, "risk_tags": ["auth"]}, role="review",
                      writer_families=("gpt",), leg_reader=reader(), catalog_rows=(),
                      policy=RoutePolicy(paused_harnesses={"agy": ()}), installed=lambda _: True)
    assert not any(leg.claude_family for leg in plan.legs)


def test_a_credential_tagged_diff_gets_the_claude_seat():
    for card in ({"risk_tags": ["secrets"]}, {"risk_tags": ["auth"]}, {"paths": ["src/auth/session.rs"]}):
        plan = review(card)
        assert plan.first_leg.id == AGY_SONNET, card
    assert not any(leg.claude_family for leg in review().legs)


def test_a_credential_diff_keeps_both_required_seats_and_adds_sonnet():
    for card in ({"risk_tags": ["secrets"]}, {"paths": ["src/auth/session.rs"]}):
        plan = review(card)
        ids = [leg.id for leg in plan.review_seats]
        assert ids[0] == AGY_SONNET and set(REVIEW_REQUIRED) <= set(ids), (card, ids)
        assert len(ids) == 3 and plan.error is None
    # A deepseek-written credential diff fences that required seat; the other stays beside Sonnet.
    plan = review({"risk_tags": ["secrets"]}, writers=("deepseek",))
    ids = [leg.id for leg in plan.review_seats]
    assert ids[0] == AGY_SONNET and REVIEW_REQUIRED[1] in ids and REVIEW_REQUIRED[0] not in ids
    # Without a credential tag the seats stay the two required ones.
    assert [leg.id for leg in review().review_seats] == list(REVIEW_REQUIRED)


def test_the_claude_seat_serves_only_when_both_required_seats_are_out():
    assert not any(leg.claude_family for leg in review(down=REQUIRED_MODELS - {BY_ID[REVIEW_REQUIRED[0]].model}).legs)
    assert [leg.id for leg in review(down=REQUIRED_MODELS).legs if leg.claude_family] == [AGY_SONNET]
    # A deepseek-written diff fences one required seat; the other still serves and Claude stays out.
    plan = review(writers=("deepseek",))
    assert plan.legs[0].id == REVIEW_REQUIRED[1]
    assert not any(leg.claude_family for leg in plan.legs)
    # A claude-written diff never gets the Claude seat, even with both required seats out.
    assert not any(leg.claude_family for leg in review(writers=("claude",), down=REQUIRED_MODELS).legs)
