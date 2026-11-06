"""D-4147: Sol-first writers, mimo-first reviews, retained policy entries."""
from __future__ import annotations

import json
import dataclasses
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

# Ordering probes include inline-only review legs, which require diff metadata.
CARD = {"estimate_tokens": 20000, "risk_tags": [],
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
FREE = ["opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free", "openrouter-free"]
SUBS = ["cx-gpt-6.1-sol", "agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash"]   # D-3667: Sol heads the subscription tier


def test_default_qwen_disabled_and_gpt_review_families():
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == "qodercli-qwen3.8-flash")
    assert leg.disabled == "D-3950 credits out; re-enable after one passing probe seat, one try per day"
    assert leg.roles == ("review",)
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",))
    assert leg.id not in {candidate.id for candidate in plan.legs}
    assert len({seat.family for seat in plan.review_seats} - {"gpt"}) >= 2
    assert all(seat.family != "gpt" for seat in plan.review_seats)


def test_explicit_disabled_qwen_refused(tmp_path):
    import asyncio
    import pytest
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused, match=r"^leg expired: qodercli-qwen3\.8-flash$"):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card=CARD, leg="qodercli-qwen3.8-flash",
            override_reason="test disabled leg", dry_run=True))


def test_writer_order_sol_family_excluded():
    plan = plan_spawn({"estimate_tokens": 20000, "risk_tags": []},
                      role="implement", exclude_families=("gpt",))
    assert plan.first_leg.id == "opencode-deepseek-v4.1-flash-free"
    assert "gpt" not in {leg.family for leg in plan.legs}
    assert {s["leg"]: s["reason"] for s in plan.skipped}[
        "cx-gpt-6.1-sol"] == "excluded_family"


def test_writer_order_privacy_excludes_training_sol():
    assert next(leg for leg in DEFAULT_LEGS if leg.id == "cx-gpt-6.1-sol").trains_on_data is False
    # The default Sol is non-training. Exercise eligibility before lead sorting
    # with a training lead and a non-training free fallback explicitly.
    legs = tuple(dataclasses.replace(leg, trains_on_data=leg.id == "cx-gpt-6.1-sol")
                 for leg in DEFAULT_LEGS)
    public = plan_spawn({"estimate_tokens": 20000, "risk_tags": []},
                        role="implement", legs=legs)
    assert public.first_leg.id == "cx-gpt-6.1-sol"
    private = plan_spawn({"estimate_tokens": 20000, "risk_tags": ["secrets"]},
                         role="implement", legs=legs)
    assert private.first_leg.id == "opencode-deepseek-v4.1-flash-free"
    assert "cx-gpt-6.1-sol" not in {leg.id for leg in private.legs}
    assert {s["leg"]: s["reason"] for s in private.skipped}[
        "cx-gpt-6.1-sol"] == "trains_on_data"


def test_cli_route_implement_gpt_first_no_claude_writer(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "implement", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in plan["legs"]]
    assert ids == SUBS[:1] + FREE + SUBS[1:] + ["deepseek"]  # D-3745
    assert "agy-sonnet-5.5" not in ids
    assert [leg["id"] for leg in plan["legs"] if leg["claude_family"]] == []


def test_route_and_refusal(tmp_path, capsys):
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}', '--role', 'implement', '--json']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['first_leg']['model'] == 'omniroute/cx-gpt-6.1-sol'  # D-3745
    assert set(plan) == {'legs', 'first_leg', 'skipped', 'p_source', 'review_seats', 'reasons', 'error', 'gaps', 'candidates'}
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert 'spawn by card' in capsys.readouterr().err


def test_review_mimo_first():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    ids = [leg.id for leg in plan.legs]
    assert ids[:4] == ["mimo-v2.6-flash", "mimo-v2.6-pro", *FREE[:2]]


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
    assert [leg.id for leg in rev.legs][:2] == ["mimo-v2.6-flash", "mimo-v2.6-pro"]


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


def test_cli_route_review_mimo_first(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "claude", "--json"]) == 0
    ids = [leg["id"] for leg in json.loads(capsys.readouterr().out)["legs"]]
    assert ids[:4] == ["mimo-v2.6-flash", "mimo-v2.6-pro", *FREE[:2]]
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
    legs = tuple(replace(table[id], roles=("review",), disabled=None) for id in ids)
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                      policy=RoutePolicy(), leg_reader=lambda *a, **k: {"state": "available", "evidence": None})
    assert [leg.id for leg in plan.legs] == ["mimo-v2.6-flash", "deepseek",
        "agy-gemini-3.8-flash", "qodercli-qwen3.8-flash", *FREE, "claude-haiku-5.5"]


def test_no_anthropic_implement_leg_removes_sonnet():
    from plangraph.planner.legs import DEFAULT_LEGS
    assert all(leg.id != "opencode-console-sonnet-5.5" for leg in DEFAULT_LEGS)
    assert all("implement" not in leg.roles for leg in DEFAULT_LEGS
                if leg.claude_family or leg.family in ("claude", "anthropic"))


def test_renamed_and_second_haiku_remain_last():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import route_card
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    haiku = table["claude-haiku-5.5"]
    legs = (replace(haiku, id="renamed-review"),
            replace(haiku, id="second-review", model="claude-haiku-next"),
            table["agy-gemini-3.8-flash"])
    assert [leg.id for leg in route_card(CARD, legs=legs, role="review").legs] == [
        "agy-gemini-3.8-flash", "renamed-review", "second-review"]


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


def test_implement_lead_independent_of_writer_order(monkeypatch):
    from plangraph.planner import legs
    assert legs.WRITER_ORDER[0] == legs.LEAD_WRITER == "cx-gpt-6.1-sol"
    monkeypatch.setattr(legs, "WRITER_ORDER", ("openrouter-free", *legs.WRITER_ORDER))
    plan = plan_spawn(CARD, role="implement",
                      leg_reader=lambda *a, **k: {"state": "ok", "evidence": None})
    assert plan.first_leg.id == legs.LEAD_WRITER


def test_writer_order_d2683_implement_first_seven():
    # D-3745: eligible Sol leads; remaining writers retain tier/WRITER_ORDER.
    c = {"estimate_tokens": 20000, "risk_tags": []}
    plan = plan_spawn(c, role="implement")
    expected = ["cx-gpt-6.1-sol", "opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free",
                "openrouter-free", "agy-gemini-3.8-flash", "mimo-v2.6-pro",
                "mimo-v2.6-flash"]
    assert [leg.id for leg in plan.legs[:len(expected)]] == expected


def test_main_policy_gpt_first_and_retained_routes():
    from plangraph.planner.route_policy import load_route_policy, DEFAULT_POLICY_PATH
    policy = load_route_policy(DEFAULT_POLICY_PATH)
    clear = lambda *a, **k: {"state": "ok", "evidence": None}
    implement = plan_spawn(CARD, role="implement", policy=policy, leg_reader=clear)
    assert implement.first_leg.id == "cx-gpt-6.1-sol"
    assert implement.legs[1].id == "openrouter-free"
    assert "agy-gemini-3.8-flash" in {leg.id for leg in implement.legs}
    for writers in (("mimo",), ("gpt",), ("claude",), ("gpt", "mimo")):
        review = plan_spawn(CARD, role="review", writer_families=writers,
                            policy=policy, leg_reader=clear)
        assert not {leg.family for leg in review.legs} & set(writers)
        assert not {leg.family for leg in review.review_seats} & set(writers)
        assert review.first_leg.family == ("deepseek" if "mimo" in writers else "mimo")
        assert {"deepseek", "glm"} <= {leg.family for leg in review.legs}
    assert {leg.id for leg in DEFAULT_LEGS} == {
        "qodercli-qwen3.8-flash", *FREE, *SUBS, "cx-gpt-6-luna", "agy-sonnet-5.5",
        "opencode-go-gemini-4-argon", "opencode-go-glm-5.2", "opencode-go-qwen3.8-max",
        "omniroute-grok-4.7", "opencode-console-muse-spark",
        "opencode-go-muse-spark", "opencode-go-deepseek-v4-flash", "deepseek", "claude-haiku-5.5"}
