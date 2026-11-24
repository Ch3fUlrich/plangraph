"""Leg request limits come from the provider registry (LE-MIMO-LEG-CAPPED-200K-PROVIDER-SAYS-1M)."""
from __future__ import annotations

import json
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn

REGISTRY = Path(__file__).resolve().parents[1] / "configuration/opencode/providers.json"
DIFF = {"repo": "/srv/repo", "base": "a1b2", "head": "c3d4"}


#: Legs whose request bound is a deliberate cost cap below the registry window.
COST_CAPPED = {"deepseek": "paid pay-as-you-go leg, only if really needed (legs.py note)"}


def _registry_by_model_id():
    """Registry limits keyed by every name a leg may use: modelID and provider/key."""
    providers = json.loads(REGISTRY.read_text())["providers"]
    out = {}
    for name, provider in providers.items():
        for key, model in provider.get("models", {}).items():
            if "limit" in model:
                out[model.get("modelID", key)] = out[f"{name}/{key}"] = model["limit"]
    return out


def _clear(model, **scope):
    return {"state": "available", "evidence": None}


def test_every_opencode_leg_with_a_registry_limit_matches_it():
    registry = _registry_by_model_id()
    checked = [leg for leg in DEFAULT_LEGS if leg.harness == "opencode" and leg.model in registry]
    assert {"mimo-v2.6-pro", "mimo-v2.6-flash"} <= {leg.id for leg in checked}
    for leg in checked:
        limit = registry[leg.model]
        bound = limit["context"] - limit.get("output", 0)
        if leg.id in COST_CAPPED:
            assert leg.max_request_tokens <= bound, leg.id
        else:
            assert leg.max_request_tokens == bound, leg.id


def test_mimo_legs_are_not_capped_at_200k():
    legs = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id in ("mimo-v2.6-pro", "mimo-v2.6-flash"):
        assert legs[leg_id].max_request_tokens > 800_000
        assert legs[leg_id].tool_mode == "inline-only"


def test_400k_review_card_offers_mimo_pro():
    card = {"estimate_tokens": 400_000, "diff_ref": DIFF}
    plan = plan_spawn(card, role="review", writer_families=("claude",), leg_reader=_clear)
    assert plan.error is None
    assert "mimo-v2.6-pro" in {leg.id for leg in plan.legs}
    assert not [s for s in plan.skipped if s["leg"] == "mimo-v2.6-pro"
                and "max_request_tokens" in str(s.get("reason"))]
