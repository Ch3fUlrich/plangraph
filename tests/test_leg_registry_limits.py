"""Leg request limits come from the provider registry (LE-MIMO-LEG-CAPPED-200K-PROVIDER-SAYS-1M)."""
from __future__ import annotations

import json
from pathlib import Path

from plangraph.planner.legs import CX_EFFORTS, DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn

REGISTRY = Path(__file__).resolve().parents[1] / "configuration/opencode/providers.json"
DIFF = {"repo": "/srv/repo", "base": "a1b2", "head": "c3d4"}


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
    from plangraph.planner.legs import POLICY_REQUEST_CAPS
    registry = _registry_by_model_id()
    checked = [leg for leg in DEFAULT_LEGS if leg.harness == "opencode" and leg.model in registry]
    assert {"mimo-v2.6-pro", "mimo-v2.6-flash", "opencode-go-glm-5.3-flash", "deepseek"} <= {leg.id for leg in checked}
    for leg in checked:
        limit = registry[leg.model]
        bound = limit["context"] - limit.get("output", 0)
        # exact: the registry bound, lowered only by a named routing-policy cap
        assert leg.max_request_tokens == min(bound, POLICY_REQUEST_CAPS.get(leg.id, bound)), leg.id


def test_research_headroom_is_routing_policy_not_a_typed_leg_limit():
    from plangraph.planner.legs import RESEARCH_REQUEST_CAP, research_route_pool
    pool = research_route_pool(DEFAULT_LEGS)
    assert pool and all(leg.max_request_tokens <= RESEARCH_REQUEST_CAP for leg in pool)
    glm = next(leg for leg in pool if leg.id == "opencode-go-glm-5.3-flash")
    assert glm.max_request_tokens == RESEARCH_REQUEST_CAP
    registry_glm = next(leg for leg in DEFAULT_LEGS if leg.id == glm.id)
    assert registry_glm.max_request_tokens > RESEARCH_REQUEST_CAP   # the leg itself keeps the registry bound


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


def test_model_without_a_registry_entry_gets_the_safe_bound():
    from plangraph.planner import legs
    assert legs._registry_request_tokens("nope", "nope") == 100_000


def test_opencode_counterparts_of_codex_legs_are_bound_by_the_registry_window():
    from plangraph.planner.legs import EXPLICIT_LEGS, opencode_counterpart
    registry = _registry_by_model_id()
    seen = 0
    for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS):
        counterpart = opencode_counterpart(leg) if leg.family == "gpt" and leg.harness == "codex" else None
        if counterpart is None:
            continue
        key = counterpart.model.removeprefix("omniroute/")
        base = next((key[:-len(e) - 1] for e in CX_EFFORTS if key.endswith("-" + e)), key)
        limit = registry.get("omniroute/" + key) or registry.get("omniroute/" + base)
        assert limit, counterpart.model
        assert counterpart.max_request_tokens <= limit["context"] - limit["output"], counterpart.id
        seen += 1
    assert seen >= 10


def test_every_registry_leg_follows_an_altered_registry_and_the_cap_applies_separately():
    """Import legs.py against a registry whose windows differ from the shipped file: a typed
    number anywhere (mimo, glm, the capped deepseek leg) cannot match the altered bounds."""
    import subprocess, sys, os, json as _json
    script = (
        "import copy, json, sys\n"
        "from plangraph.routing_support import opencode_overlay as o\n"
        "real = o.load_root_providers\n"
        "def altered(path=None):\n"
        "    providers = real(path)\n"
        "    sizes = {'mimo-v2.6-flash': (300000, 20000), 'mimo-v2.6-pro': (310000, 20000),\n"
        "             'opencode-go/glm-5.3-flash': (320000, 20000), 'deepseek-direct-flash': (60000, 10000)}\n"
        "    for provider in providers.values():\n"
        "        for key, model in provider.get('models', {}).items():\n"
        "            if key in sizes:\n"
        "                model['limit'] = {'context': sizes[key][0], 'output': sizes[key][1]}\n"
        "    return providers\n"
        "o.load_root_providers = altered\n"
        "from plangraph.planner.legs import DEFAULT_LEGS, POLICY_REQUEST_CAPS\n"
        "print(json.dumps({l.id: l.max_request_tokens for l in DEFAULT_LEGS}))\n"
        "print(json.dumps(POLICY_REQUEST_CAPS))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REGISTRY.parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    bounds, caps = (_json.loads(line) for line in out.stdout.strip().splitlines())
    assert bounds["mimo-v2.6-flash"] == 280_000 and bounds["mimo-v2.6-pro"] == 290_000
    assert bounds["opencode-go-glm-5.3-flash"] == 300_000
    # capability below the cap: the registry decides (50,000), not the cap (100,000)
    assert caps["deepseek"] == 100_000 and bounds["deepseek"] == 50_000
