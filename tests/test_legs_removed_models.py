"""Pins for SPAWNER-LEGS-OVH-DROP (D-3368): dropped operator-removed legs.

(a) none of the five ids is in {l.id for l in DEFAULT_LEGS};
(b) no DEFAULT_LEGS model is in resolver.OPERATOR_REMOVED_MODELS;
(c) route() for review and implement roles never returns one of the five ids.
"""
from __future__ import annotations

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support import resolver
from plangraph.spawner.service import SpawnService

REMOVED_LEG_IDS = frozenset({
    "opencode-zen-free",
    "or-north-mini-code-free",
    "or-laguna-s-2.1-free",
    "or-nemotron-3-super-free",
    "ovh-gpt-oss-120b",
})


def test_removed_leg_ids_not_in_default_legs():
    active_ids = {l.id for l in DEFAULT_LEGS}
    for leg_id in REMOVED_LEG_IDS:
        assert leg_id not in active_ids
    assert not (REMOVED_LEG_IDS & active_ids)


def test_no_default_legs_model_in_operator_removed_models():
    assert resolver.OPERATOR_REMOVED_MODELS
    for leg in DEFAULT_LEGS:
        assert leg.model not in resolver.OPERATOR_REMOVED_MODELS


@pytest.mark.parametrize("role", ["implement", "review"])
def test_route_never_returns_removed_leg_ids(tmp_path, role):
    svc = SpawnService(state=tmp_path / "state")
    card = {"estimate_tokens": 1000}
    writer_families = ("anthropic",) if role == "review" else ()

    plan = svc.route(card, role=role, writer_families=writer_families)
    assert plan.first_leg.id not in REMOVED_LEG_IDS
    assert not ({l.id for l in plan.legs} & REMOVED_LEG_IDS)
    if getattr(plan, "review_seats", None):
        assert not ({l.id for l in plan.review_seats} & REMOVED_LEG_IDS)

    direct = plan_spawn(card, role=role, writer_families=writer_families)
    assert direct.first_leg.id not in REMOVED_LEG_IDS
    assert not ({l.id for l in direct.legs} & REMOVED_LEG_IDS)
    if getattr(direct, "review_seats", None):
        assert not ({l.id for l in direct.review_seats} & REMOVED_LEG_IDS)


@pytest.mark.parametrize("writer", ["claude", "gpt", "deepseek", "qwen", "gemini", "mimo"])
def test_route_review_across_writer_families_never_returns_removed_leg_ids(tmp_path, writer):
    svc = SpawnService(state=tmp_path / "state")
    card = {"estimate_tokens": 1000}
    plan = svc.route(card, role="review", writer_families=(writer,))
    assert plan.first_leg.id not in REMOVED_LEG_IDS
    assert not ({l.id for l in plan.legs} & REMOVED_LEG_IDS)
    if getattr(plan, "review_seats", None):
        assert not ({l.id for l in plan.review_seats} & REMOVED_LEG_IDS)


@pytest.mark.parametrize("tokens", [1000, 40000, 90000, 150000])
def test_route_implement_across_token_scales_never_returns_removed_leg_ids(tmp_path, tokens):
    svc = SpawnService(state=tmp_path / "state")
    card = {"estimate_tokens": tokens}
    plan = svc.route(card, role="implement")
    assert plan.first_leg.id not in REMOVED_LEG_IDS
    assert not ({l.id for l in plan.legs} & REMOVED_LEG_IDS)
