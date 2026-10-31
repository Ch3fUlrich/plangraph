"""Free legs lead every route (SPAWNER-ROUTE-FREE-FIRST).

For a public standard card ``spawner.cli route`` lists the serving free legs
(opencode/*-free, openrouter-free) before the subscription legs, for implement
and review alike; free legs the route policy bans stay skipped; agy is skipped
while out of quota (leg_state); no anthropic/claude writer leg appears for
implement (agy-sonnet-5.5 is orchestrate-only).
"""
from __future__ import annotations

import json

from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

CARD = {"estimate_tokens": 20000, "risk_tags": []}
FREE = ["opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free", "openrouter-free"]
SUBS = ["agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash", "cx-gpt-6.1-sol"]


def test_cli_route_implement_free_first_no_claude_writer(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "implement", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in plan["legs"]]
    assert ids == FREE + SUBS + ["deepseek"]
    assert "agy-sonnet-5.5" not in ids
    assert not any(leg["claude_family"] or leg["family"] == "claude" for leg in plan["legs"])


def test_review_free_first():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    ids = [leg.id for leg in plan.legs]
    assert ids[:3] == ["qodercli-qwen3.8-flash", *FREE[:2]] and ids[3] == "agy-gemini-3.8-flash"


def test_policy_banned_free_legs_stay_skipped(tmp_path, monkeypatch):
    path = tmp_path / "route-policy.json"
    path.write_text(json.dumps({"banned_families": ["deepseek"], "off_families": ["glm"]}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    imp = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in imp.legs] == ["openrouter-free"] + SUBS
    reasons = {s["leg"]: s["reason"] for s in imp.skipped}
    assert reasons["opencode-deepseek-v4.1-flash-free"] == "banned_family"
    assert reasons["opencode-glm-5.3-flash-free"] == "off_family"
    rev = plan_spawn(CARD, role="review", writer_families=("claude",))
    assert [leg.id for leg in rev.legs][:2] == ["qodercli-qwen3.8-flash", "agy-gemini-3.8-flash"]


def test_agy_skipped_while_out_of_quota():
    def reader(model, **kwargs):
        limited = model in ("gemini-3.8-flash", "agy/gemini-3.8-flash")
        return {"state": "limited" if limited else "available",
                "evidence": {"unavailable_until": 1000} if limited else None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    ids = [leg.id for leg in plan.legs]
    assert ids == FREE + ["mimo-v2.6-pro", "mimo-v2.6-flash", "cx-gpt-6.1-sol", "deepseek"]
    assert "agy-gemini-3.8-flash" not in ids
    assert {s["leg"]: s["reason"] for s in plan.skipped}["agy-gemini-3.8-flash"] == "limited"


def test_cli_route_review_free_first(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "claude", "--json"]) == 0
    ids = [leg["id"] for leg in json.loads(capsys.readouterr().out)["legs"]]
    assert ids[:3] == ["qodercli-qwen3.8-flash", *FREE[:2]]
    assert ids.index("agy-gemini-3.8-flash") > 2
