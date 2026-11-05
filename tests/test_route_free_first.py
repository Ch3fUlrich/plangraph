"""D-3901: Sol-first writers, free-first reviews, disabled anthropic writers."""
from __future__ import annotations

import json

from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

CARD = {"estimate_tokens": 20000, "risk_tags": []}
FREE = ["opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free", "openrouter-free"]
SUBS = ["cx-gpt-6.1-sol", "agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash"]   # D-3667: Sol heads the subscription tier


def test_cli_route_implement_free_first_no_claude_writer(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "implement", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in plan["legs"]]
    assert ids == SUBS[:1] + FREE + SUBS[1:] + ["deepseek"]  # D-3745
    assert "agy-sonnet-5.5" not in ids
    assert [leg["id"] for leg in plan["legs"] if leg["claude_family"]] == []


def test_review_free_first():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    ids = [leg.id for leg in plan.legs]
    assert ids[:3] == ["qodercli-qwen3.8-flash", *FREE[:2]] and ids[3] == "mimo-v2.6-flash"


def test_policy_banned_free_legs_stay_skipped(tmp_path, monkeypatch):
    path = tmp_path / "route-policy.json"
    path.write_text(json.dumps({"banned_families": ["deepseek"], "off_families": ["glm"]}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    imp = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in imp.legs] == SUBS[:1] + ["openrouter-free"] + SUBS[1:]  # D-3745
    reasons = {s["leg"]: s["reason"] for s in imp.skipped}
    assert reasons["opencode-deepseek-v4.1-flash-free"] == "banned_family"
    assert reasons["opencode-glm-5.3-flash-free"] == "off_family"
    rev = plan_spawn(CARD, role="review", writer_families=("claude",))
    assert [leg.id for leg in rev.legs][:2] == ["qodercli-qwen3.8-flash", "mimo-v2.6-flash"]


def test_agy_skipped_while_out_of_quota():
    def reader(model, **kwargs):
        limited = model in ("gemini-3.8-flash", "agy/gemini-3.8-flash")
        return {"state": "limited" if limited else "available",
                "evidence": {"unavailable_until": 1000} if limited else None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    ids = [leg.id for leg in plan.legs]
    assert ids == ["cx-gpt-6.1-sol"] + FREE + ["mimo-v2.6-pro", "mimo-v2.6-flash",  # D-3745
                          "deepseek"]
    assert "agy-gemini-3.8-flash" not in ids
    assert {s["leg"]: s["reason"] for s in plan.skipped}["agy-gemini-3.8-flash"] == "limited"


def test_cli_route_review_free_first(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "claude", "--json"]) == 0
    ids = [leg["id"] for leg in json.loads(capsys.readouterr().out)["legs"]]
    assert ids[:3] == ["qodercli-qwen3.8-flash", *FREE[:2]]
    assert ids.index("agy-gemini-3.8-flash") > 2


def test_route_sol_unavailable_keeps_tier_writer_order():
    # D-3745: an ineligible lead does not change the fallback ordering.
    def reader(model, **kwargs):
        return {"state": "down" if model == "omniroute/cx-gpt-6.1-sol" else "available", "evidence": None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    assert [leg.id for leg in plan.legs] == FREE + SUBS[1:] + ["deepseek"]
    assert {s["leg"]: s["reason"] for s in plan.skipped}[SUBS[0]] == "down"


def test_fixed_review_order():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.route_policy import RoutePolicy
    # Fixed table, including normally implement-only free and paid fallbacks.
    ids = ["claude-haiku-5.5", "deepseek", "agy-gemini-3.8-flash",
           "mimo-v2.6-flash", "qodercli-qwen3.8-flash", *FREE]
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = tuple(replace(table[id], roles=("review",)) for id in ids)
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                      policy=RoutePolicy(), leg_reader=lambda *a, **k: {"state": "available", "evidence": None})
    assert [leg.id for leg in plan.legs] == ["qodercli-qwen3.8-flash", *FREE,
        "mimo-v2.6-flash", "deepseek", "agy-gemini-3.8-flash", "claude-haiku-5.5"]


def test_no_anthropic_implement_leg_retains_sonnet():
    from plangraph.planner.legs import DEFAULT_LEGS
    assert any(leg.id == "opencode-console-sonnet-5.5" for leg in DEFAULT_LEGS)
    assert all("implement" not in leg.roles for leg in DEFAULT_LEGS
               if leg.claude_family or leg.family in ("claude", "anthropic"))


def test_fixed_implement_sol_across_tiers():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS, TIERS
    from plangraph.planner.routing import route_card
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    for tier in TIERS:
        legs = (table[FREE[2]], replace(table[SUBS[0]], tier=tier),
                table[FREE[0]], table[SUBS[2]])
        expected = [SUBS[0], FREE[0], FREE[2], SUBS[2]]
        assert [leg.id for leg in route_card(CARD, legs=legs, role="implement").legs] == expected
        fallback = route_card(CARD, legs=legs, role="implement", unavailable=(SUBS[0],))
        assert [leg.id for leg in fallback.legs] == expected[1:]
