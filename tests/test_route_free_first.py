"""D-5212: Sol xhigh then Gemini writers; other legs retain cost-tier ordering."""
from __future__ import annotations

import json
import dataclasses
from pathlib import Path

import pytest

from plangraph.planner.legs import (DEFAULT_LEGS, REVIEW_ORDER, REVIEW_MIMO_PRO, REVIEW_MIMO_FLASH,
                                    REVIEW_HYDROGEN, REVIEW_NEON, REVIEW_BEAM)
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")

# Ordering probes include inline-only review legs, which require diff metadata.
CARD = {"estimate_tokens": 20000, "risk_tags": [],
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
FREE = ["opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free", "openrouter-free"]
DISABLED_FREE = {"opencode-glm-5.3-flash-free"}
ACTIVE_FREE = ["opencode-deepseek-v4.1-flash-free", "openrouter-free"]
SUBS = ["cx-gpt-6.2-sol-medium", "agy-gemini-4-hydrogen-high", "mimo-v2.6-pro", "mimo-v2.6-flash"]
WRITERS = [SUBS[0], "cx-gpt-6.2-astra", "agy-gemini-4-hydrogen-high",
           "opencode-deepseek-v4.1-flash-free", "opencode-go-deepseek-v4-flash",
           "opencode-go-glm-5.3-flash", "openrouter-free", "mimo-v2.6-flash", "mimo-v2.6-pro",
           "omniroute-deepseek-flash", "omniroute-muse-spark", "deepseek"]

@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [])
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))

def test_main_zen_disables_retained_exactly():
    reason = "D-6310: opencode/glm-5.3-flash-free not served by opencode models (use opencode-go/glm-5.3-flash); never remove (D-3828)"
    for leg in DEFAULT_LEGS:
        if leg.id in DISABLED_FREE:
            assert leg.disabled == reason
            assert leg.expires is None

def test_explicit_catalog_snapshot_never_reads_ambient_catalog(monkeypatch):
    from plangraph.routing_support import resolver
    def forbidden():
        pytest.fail("explicit catalog snapshot must not read the host catalog")
    monkeypatch.setattr(resolver, "get_catalog_models", forbidden)
    sol = next(leg for leg in DEFAULT_LEGS if leg.id == SUBS[0])
    plan = plan_spawn(CARD, role="implement", legs=(sol,),
                      catalog_rows=({"id": sol.model, "tier": "free", "available": 1, "active": True},))
    assert plan.first_leg.id == sol.id
    assert plan.first_leg.tier == "free"

def test_default_plan_reads_catalog_once(monkeypatch):
    from plangraph.routing_support import resolver
    calls = []
    def catalog():
        calls.append(True)
        return []
    monkeypatch.setattr(resolver, "get_catalog_models", catalog)
    result = plan_spawn(CARD, role="implement", catalog_rows=None)
    assert result.legs
    assert calls == [True]


def test_explicit_catalog_snapshot_preserves_admission():
    from plangraph.planner.routing import SpawnRefused
    sol = next(leg for leg in DEFAULT_LEGS if leg.id == SUBS[0])
    unknown = dataclasses.replace(sol, model="test/unregistered")
    with pytest.raises(SpawnRefused, match="unknown model: test/unregistered"):
        plan_spawn(CARD, role="implement", legs=(unknown,), catalog_rows=())
    with pytest.raises(SpawnRefused, match="unknown model:"):
        plan_spawn(CARD, role="implement", legs=(sol,), catalog_rows=(
            dict(id=sol.model, tier="free", available=0, active=False),))


def test_fixed_rows_ignore_conflicting_ambient_catalog(monkeypatch):
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [
        dict(id="opencode/big-pickle", tier="paid", available=1, active=True)])
    result = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in result.legs] == WRITERS
    low = plan_spawn(dict(CARD, risk="low", is_pure_doc=True), role="implement")
    assert next(leg for leg in low.legs if leg.id == "opencode-big-pickle").tier == "free"


@pytest.fixture(autouse=True)
def isolated_utility_inputs(tmp_path, monkeypatch):
    from plangraph.routing_support import catalog_db
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", tmp_path / "absent.db")
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))


@pytest.fixture(autouse=True)
def fixed_catalog(monkeypatch):
    """Exact-order probes use declared tiers, never the host's catalog snapshot."""
    monkeypatch.setitem(plan_spawn.__kwdefaults__, "catalog_rows", ())


def test_default_qwen_disabled_and_gpt_review_families():
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == "qodercli-qwen3.8-flash")
    assert leg.disabled == "D-4744 operator disabled, not a route option"
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
    with pytest.raises(SpawnRefused, match=r"^leg disabled: qodercli-qwen3\.8-flash: D-4744 operator disabled, not a route option; override cannot enable a disabled leg$"):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card=CARD, leg="qodercli-qwen3.8-flash",
            override_reason="test disabled leg", dry_run=True))


def test_writer_order_sol_family_excluded():
    plan = plan_spawn({"estimate_tokens": 20000, "risk_tags": []},
                      role="implement", exclude_families=("gpt",))
    assert plan.first_leg.id == "agy-gemini-4-hydrogen-high"
    assert "gpt" not in {leg.family for leg in plan.legs}
    assert {s["leg"]: s["reason"] for s in plan.skipped}[
        SUBS[0]] == "excluded_family"


def test_writer_order_privacy_excludes_training_sol():
    assert next(leg for leg in DEFAULT_LEGS if leg.id == SUBS[0]).trains_on_data is False
    # The default Sol is non-training. Exercise eligibility before lead sorting
    # with a training lead and a non-training Gemini fallback explicitly.
    legs = tuple(dataclasses.replace(leg, trains_on_data=leg.id == SUBS[0])
                 for leg in DEFAULT_LEGS)
    public = plan_spawn({"estimate_tokens": 20000, "risk_tags": []},
                        role="implement", legs=legs)
    assert public.first_leg.id == SUBS[0]
    private = plan_spawn({"estimate_tokens": 20000, "risk_tags": ["secrets"]},
                         role="implement", legs=legs)
    assert private.first_leg.id == "cx-gpt-6.2-astra"
    assert SUBS[0] not in {leg.id for leg in private.legs}
    assert {s["leg"]: s["reason"] for s in private.skipped}[
        SUBS[0]] == "trains_on_data"


def test_cli_route_implement_gpt_first_no_claude_writer(tmp_path, capsys):
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "implement", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in plan["legs"]]
    assert ids == WRITERS
    assert "agy-sonnet-5.5" not in ids and "agy-opus-5.5" not in ids
    assert [leg["id"] for leg in plan["legs"] if leg["claude_family"]] == []


def test_route_and_refusal(tmp_path, capsys):
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}', '--role', 'implement', '--json']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['first_leg']['model'] == 'omniroute/cx-gpt-6.2-sol-medium'
    assert plan['first_leg']['effort'] == 'medium'
    assert set(plan) == {'legs', 'first_leg', 'skipped', 'p_source', 'review_seats', 'reasons', 'error', 'gaps', 'candidates', 'placement', 'agy_quota_states'}
    assert plan['placement'] == {'host': None, 'reason': 'placement_not_evaluated', 'free_mb': None}
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert 'spawn by card' in capsys.readouterr().err


def test_review_order_rule_350_mimo_pro_then_flash_then_326():
    # Rule 3.58 / D-6366: agy argon first, then mimo-v2.6-pro, then the OpenCode
    # DeepSeek legs and glm-5.3-flash; neon, Sonnet high and mimo flash follow.
    assert REVIEW_ORDER == (
        "agy-gemini-4-argon-medium",
        "mimo-v2.6-pro",
        "opencode-deepseek-v4.1-flash-free",
        "opencode-go-deepseek-v4-flash",
        "opencode-go-glm-5.3-flash",
        "agy-gemini-4-neon-medium",
        "claude-sonnet-5-5-high",
        "mimo-v2.6-flash",
    )
    plan = plan_spawn(CARD, role="review", writer_families=("openai",),
                      leg_reader=lambda model, **kwargs: {"state": "available", "evidence": None})
    # An OpenAI writer gets exactly the operator order.
    assert [leg.id for leg in plan.legs] == list(REVIEW_ORDER)
    assert plan.legs[0].model == "gemini-4-argon-medium"
    # A Gemini writer skips argon and neon (same family) and starts on mimo-pro.
    gem_plan = plan_spawn(CARD, role="review", writer_families=("gemini",),
                          leg_reader=lambda model, **kwargs: {"state": "available", "evidence": None})
    assert [leg.id for leg in gem_plan.legs] == [
        "mimo-v2.6-pro",
        "opencode-deepseek-v4.1-flash-free",
        "opencode-go-deepseek-v4-flash",
        "opencode-go-glm-5.3-flash",
        "claude-sonnet-5-5-high",
        "mimo-v2.6-flash",
        "cx-gpt-6-luna",
        "agy-gpt-oss-120b",
    ]


def test_review_mimo_first():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    ids = [leg.id for leg in plan.legs]
    assert ids == [*[id for id in REVIEW_ORDER if id != "claude-sonnet-5-5-high"],
                   "cx-gpt-6-luna", "agy-gpt-oss-120b"]
    assert DISABLED_FREE.isdisjoint(ids)
    assert ACTIVE_FREE == [leg.id for leg in DEFAULT_LEGS if leg.id in FREE and not leg.disabled]


def test_policy_banned_free_legs_stay_skipped(tmp_path, monkeypatch):
    path = tmp_path / "route-policy.json"
    path.write_text(json.dumps({"banned_families": ["deepseek"], "off_families": ["glm"]}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    imp = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in imp.legs] == [id for id in WRITERS if id not in
        (FREE[0], "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash",
         "deepseek", "omniroute-deepseek-flash")]
    rev = plan_spawn(CARD, role="review", writer_families=("claude",))
    reasons = {s["leg"]: s["reason"] for s in rev.skipped}
    assert reasons[FREE[1]].startswith("disabled: D-6310")
    assert reasons["omniroute-deepseek-flash"] == "banned_family"
    assert [leg.id for leg in rev.legs] == [
        "agy-gemini-4-argon-medium", REVIEW_MIMO_PRO, REVIEW_NEON, REVIEW_MIMO_FLASH,
        "cx-gpt-6-luna", "agy-gpt-oss-120b"]
    assert reasons[FREE[0]] == "banned_family"
    assert reasons["opencode-go-deepseek-v4-flash"] == "banned_family"
    assert reasons["opencode-go-glm-5.3-flash"] == "off_family"


def test_agy_skipped_while_out_of_quota():
    def reader(model, **kwargs):
        limited = model in ("gemini-4-hydrogen-high", "agy/gemini-4-hydrogen-high",
                            "gemini-4-neon-medium", "agy/gemini-4-neon-medium")
        return {"state": "limited" if limited else "available",
                "evidence": {"unavailable_until": 1000} if limited else None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    ids = [leg.id for leg in plan.legs]
    assert ids == [id for id in WRITERS if id != SUBS[1]]
    assert "agy-gemini-4-hydrogen-high" not in ids
    review = plan_spawn(CARD, role="review", writer_families=("gpt",), leg_reader=reader)
    assert {s["leg"]: s["reason"] for s in review.skipped}["agy-gemini-4-neon-medium"] == "limited"


def test_cli_route_review_mimo_first(tmp_path, capsys, monkeypatch):
    # Ordering without measurements must not depend on the host's catalog DB.
    from plangraph.routing_support import catalog_db
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", tmp_path / "absent.db")
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "claude", "--json"]) == 0
    ids = [leg["id"] for leg in json.loads(capsys.readouterr().out)["legs"]]
    assert ids == [*[id for id in REVIEW_ORDER if id != "claude-sonnet-5-5-high"],
                   "cx-gpt-6-luna", "agy-gpt-oss-120b"]
    assert DISABLED_FREE.isdisjoint(ids)
    assert ids[:2] == ["agy-gemini-4-argon-medium", "mimo-v2.6-pro"]


def test_route_sol_unavailable_keeps_tier_writer_order():
    # D-3745: an ineligible lead does not change the fallback ordering.
    def reader(model, **kwargs):
        return {"state": "down" if model == "omniroute/cx-gpt-6.2-sol-medium" else "available", "evidence": None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    assert [leg.id for leg in plan.legs] == WRITERS[1:]
    assert {s["leg"]: s["reason"] for s in plan.skipped}[SUBS[0]] == "down"


def test_fixed_review_order():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.route_policy import RoutePolicy
    # Fixed table, including normally implement-only free and paid fallbacks.
    ids = ["claude-haiku-5.5", "deepseek", "agy-gemini-4-neon-medium",
           "mimo-v2.6-flash", "qodercli-qwen3.8-flash", *FREE]
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = tuple(replace(table[id], roles=("review",), disabled=None) for id in ids)
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                      policy=RoutePolicy(), leg_reader=lambda *a, **k: {"state": "available", "evidence": None})
    assert [leg.id for leg in plan.legs] == ["qodercli-qwen3.8-flash", *FREE,
        "mimo-v2.6-flash", "agy-gemini-4-neon-medium", "claude-haiku-5.5", "deepseek"]


def test_no_anthropic_implement_leg_removes_sonnet():
    from plangraph.planner.legs import DEFAULT_LEGS
    assert all(leg.id != "opencode-console-sonnet-5.5" for leg in DEFAULT_LEGS)
    assert {leg.id for leg in DEFAULT_LEGS if "implement" in leg.roles
            and (leg.claude_family or leg.family in ("claude", "anthropic"))} == set()
    assert next(leg for leg in DEFAULT_LEGS if leg.id == "agy-sonnet-5.5").roles == ("orchestrate", "review")
    assert next(leg for leg in DEFAULT_LEGS if leg.id == "agy-opus-5.5").roles == ("review",)


def test_renamed_and_second_haiku_remain_last():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import route_card
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    haiku = table["claude-haiku-5.5"]
    legs = (replace(haiku, id="renamed-review"),
            replace(haiku, id="second-review", model="claude-haiku-next"),
            table["agy-gemini-4-neon-medium"])
    assert [leg.id for leg in route_card(CARD, legs=legs, role="review").legs] == [
        "agy-gemini-4-neon-medium", "renamed-review", "second-review"]


@pytest.mark.parametrize("benchmark", [None, {}])
def test_haiku_last_across_tiers_and_measurements(monkeypatch, benchmark):
    from plangraph.planner.route_policy import RoutePolicy
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [])
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = (dataclasses.replace(table["claude-haiku-5.5"], tier="free"),
            dataclasses.replace(table["agy-opus-5.5"], tier="paid"),
            dataclasses.replace(table["agy-gemini-4-neon-medium"], tier="paid"))
    if benchmark is not None:
        benchmark = {leg.model: {"aa_intelligence": 100 - i * 40,
                     "cost_per_task_usd": 0, "pool_burn_weight": 0}
                     for i, leg in enumerate(legs)}
    result = plan_spawn(CARD, role="review", legs=legs, benchmark=benchmark,
                        writer_families=("gpt",), policy=RoutePolicy(),
                        leg_reader=lambda *a, **k: {"state": "ok", "evidence": None})
    assert {leg.id for leg in result.legs} == {leg.id for leg in legs}
    # Main's reviewed tier boundary wins even for a catalog-repriced Haiku;
    # it remains last within a shared tier when no measurements supersede it.
    assert [leg.id for leg in result.legs] == ["claude-haiku-5.5", "agy-opus-5.5", "agy-gemini-4-neon-medium"]
    same_tier = tuple(dataclasses.replace(leg, tier="subscription") for leg in legs)
    fallback = plan_spawn(CARD, role="review", legs=same_tier,
                          writer_families=("gpt",), policy=RoutePolicy(),
                          leg_reader=lambda *a, **k: {"state": "ok", "evidence": None})
    assert fallback.legs[-1].id == "claude-haiku-5.5"


@pytest.mark.parametrize("family,flag", [("claude", False), ("anthropic", False), ("other", True)])
def test_custom_claude_implement_roles_cannot_route(family, flag):
    from plangraph.planner.route_policy import RoutePolicy
    from plangraph.planner.routing import route_card
    safe = next(leg for leg in DEFAULT_LEGS if leg.id == SUBS[0])
    blocked = dataclasses.replace(safe, id="custom-claude", family=family, claude_family=flag)
    assert route_card(CARD, legs=(blocked, safe), role="implement").legs == (safe,)
    result = plan_spawn(CARD, role="implement", legs=(blocked, safe), policy=RoutePolicy(),
                        leg_reader=lambda *a, **k: {"state": "ok", "evidence": None})
    assert result.legs == (safe,)
    assert any(row["leg"] == blocked.id and row["reason"] == "excluded_family"
               for row in result.skipped)


def test_fixed_implement_sol_across_tiers():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS, TIERS
    from plangraph.planner.routing import route_card
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    SUBS = ["cx-gpt-6.2-sol-medium", "agy-gemini-4-hydrogen-high", "mimo-v2.6-pro", "mimo-v2.6-flash"]
    for tier in TIERS:
        legs = (replace(table[FREE[2]], disabled=None), replace(table[SUBS[0]], tier=tier),
                replace(table[FREE[0]], disabled=None), table[SUBS[2]])
        # route_card is a low-level ordering probe; plan_spawn filters roles.
        expected = [SUBS[0], FREE[0], FREE[2], SUBS[2]]
        assert [leg.id for leg in route_card(CARD, legs=legs, role="implement").legs] == expected
        fallback = route_card(CARD, legs=legs, role="implement", unavailable=(SUBS[0],))
        assert [leg.id for leg in fallback.legs] == [FREE[0], FREE[2], SUBS[2]]
        for gemini_tier in TIERS:
            writers = (*legs, replace(table[SUBS[1]], tier=gemini_tier))
            assert [leg.id for leg in route_card(CARD, legs=writers, role="implement").legs] == [
                SUBS[0], SUBS[1], FREE[0], FREE[2], SUBS[2]]
            fallback = route_card(CARD, legs=writers, role="implement", unavailable=(SUBS[0],))
            assert [leg.id for leg in fallback.legs] == [SUBS[1], FREE[0], FREE[2], SUBS[2]]
        # Neither writer promotion may erase the subscription/paid non-writer slots.
        for other_tier in TIERS:
            other = replace(table[SUBS[3]], tier=other_tier)
            rest = (replace(table["deepseek"], disabled=None), table[SUBS[2]], other,
                    replace(table[FREE[2]], disabled=None))
            expected_rest = {
                "trial": [other.id, FREE[2], SUBS[2], "deepseek"],
                "free": [other.id, FREE[2], SUBS[2], "deepseek"],
                "subscription": [FREE[2], SUBS[2], other.id, "deepseek"],
                "paid": [FREE[2], SUBS[2], "deepseek", other.id],
            }[other_tier]
            writers = (*rest, replace(table[SUBS[0]], tier=tier),
                       replace(table[SUBS[1]], tier=tier))
            for unavailable, prefix in (((), SUBS[:2]), ((SUBS[0],), SUBS[1:2]),
                                        (tuple(SUBS[:2]), [])):
                route = route_card(CARD, legs=writers, role="implement", unavailable=unavailable)
                assert [leg.id for leg in route.legs] == prefix + expected_rest


def test_implement_lead_independent_of_writer_order(monkeypatch):
    from plangraph.planner import legs
    assert legs.WRITER_ORDER[0] == legs.LEAD_WRITER == SUBS[0]
    monkeypatch.setattr(legs, "WRITER_ORDER", ("openrouter-free", *legs.WRITER_ORDER))
    plan = plan_spawn(CARD, role="implement",
                      leg_reader=lambda *a, **k: {"state": "ok", "evidence": None})
    assert plan.first_leg.id == SUBS[0]  # D-5206: the designated lead is independent.


def test_writer_order_d2683_implement_first_seven(monkeypatch):
    from mechanics_legs import install_fallthrough_legs
    install_fallthrough_legs(monkeypatch)
    # D-6310 / rule 3.53: Sol medium, Astra, then Hydrogen, then opencode fallbacks.
    c = {"estimate_tokens": 20000, "risk_tags": []}
    expected = [SUBS[0], 'cx-gpt-6.2-astra', 'agy-gemini-4-hydrogen-high',
                'opencode-deepseek-v4.1-flash-free', 'opencode-go-deepseek-v4-flash',
                'opencode-go-glm-5.3-flash', 'opencode-glm-5.3-flash-free', 'openrouter-free',
                'agy-gemini-4-neon-medium', 'mimo-v2.6-flash', 'mimo-v2.6-pro',
                'omniroute-deepseek-flash', 'omniroute-muse-spark', 'deepseek']
    for free_tier in ("trial", "free", "subscription", "paid"):
        plan = plan_spawn(c, role="implement", catalog_rows=(
            {"id": "openrouter/free", "tier": free_tier, "available": 1, "active": True},))
        repriced = [id for id in expected if id != "openrouter-free"]
        slot = {"trial": 6, "free": 7, "subscription": 7, "paid": 12}[free_tier]
        repriced.insert(slot, "openrouter-free")
        assert [leg.id for leg in plan.legs] == repriced


def test_main_policy_gpt_first_and_retained_routes():
    from plangraph.planner.route_policy import load_route_policy, DEFAULT_POLICY_PATH
    policy = load_route_policy(DEFAULT_POLICY_PATH)
    clear = lambda *a, **k: {"state": "ok", "evidence": None}
    implement = plan_spawn(CARD, role="implement", policy=policy, leg_reader=clear)
    assert implement.first_leg.id == SUBS[0]
    assert [leg.id for leg in implement.legs] == WRITERS
    for writers in (("mimo",), ("gpt",), ("claude",), ("gpt", "mimo")):
        review = plan_spawn(CARD, role="review", writer_families=writers,
                            policy=policy, leg_reader=clear)
        assert not {leg.family for leg in review.legs} & set(writers)
        assert not {leg.family for leg in review.review_seats} & set(writers)
        assert review.first_leg.id == ("mimo-v2.6-pro" if "gemini" in writers else "agy-gemini-4-argon-medium")
        assert "deepseek" in {leg.family for leg in review.legs}
        assert "glm" in {leg.family for leg in review.legs}
    assert {leg.id for leg in DEFAULT_LEGS} == {
        "qodercli-qwen3.8-flash", *FREE, *SUBS, "cx-gpt-6.2-astra",
        "agy-gemini-4-hydrogen-medium", "cx-gpt-6-luna", "agy-sonnet-5.5",
        "agy-gemini-4-neon-medium", "opencode-flint-40b-free", "opencode-big-pickle",
        "agy-opus", "agy-opus-5.5", "omniroute-deepseek-flash", "omniroute-muse-spark",
        "opencode-go-beam-free", "opencode-go-glm-5.3-flash", "opencode-go-qwen3.8-max",
        "omniroute-grok-4.7", "opencode-console-muse-spark",
        "opencode-go-muse-spark", "opencode-go-deepseek-v4-flash", "deepseek",
        "claude-haiku-5.5", "agy-claude-opus", "agy-gpt-oss-120b"}


def test_codex_writer_aliases_and_cross_family_review_routes():
    plans = [plan_spawn(CARD, role='review', writer_families=[name])
             for name in ('codex', 'cx-gpt-6.2-sol', 'gpt')]
    assert all(p.legs == plans[0].legs and p.review_seats == plans[0].review_seats for p in plans)
    ids = {l.id for l in plans[0].legs}
    assert 'omniroute-deepseek-flash' not in ids
    assert [l.id for l in plans[0].legs] == list(REVIEW_ORDER)
    assert any(l.family == 'gemini' for l in plans[0].legs)
    assert any(l.family == 'deepseek' for l in plans[0].legs)
    assert any(l.family == 'glm' for l in plans[0].legs)
    deepseek = plan_spawn(CARD, role='review', writer_families=['deepseek'])
    assert all(l.family != 'deepseek' for l in deepseek.legs)
    claude = plan_spawn(CARD, role='review', writer_families=['claude'])
    assert any(l.id.startswith('cx-gpt-') for l in claude.legs)


def test_d6310_route_orders_low_risk_and_families():
    from plangraph.planner.legs import CX_EFFORTS, EXPLICIT_WRITERS, spawnable_legs
    from plangraph.planner.route_policy import load_route_policy
    from plangraph.registry_columns import PROVIDER_HARNESS
    from plangraph.retired_models import is_retired, RetiredModelError, check_retired
    from plangraph.routing_support.catalog_seed import SEED_PROVIDERS
    from plangraph.routing_support.resolver import MODEL_FAMILIES, detect_model_family

    # Policy & provider invariants
    policy = load_route_policy()
    assert 'deepseek' not in policy.banned_families and 'glm' not in policy.off_families
    assert 'opencode' in PROVIDER_HARNESS
    assert any(p['provider_id'] == 'opencode' for p in SEED_PROVIDERS)
    assert all(l.disabled is None for l in spawnable_legs() if l.model.startswith('opencode-go/'))

    # Implement and review exact target orders with efforts
    imp = plan_spawn({'estimate_tokens': 40000}, role='implement')
    assert [(l.id, l.effort) for l in imp.legs[:6]] == [
        ('cx-gpt-6.2-sol-medium', 'medium'),
        ('cx-gpt-6.2-astra', None),
        ('agy-gemini-4-hydrogen-high', 'high'),
        ('opencode-deepseek-v4.1-flash-free', None),
        ('opencode-go-deepseek-v4-flash', None),
        ('opencode-go-glm-5.3-flash', None),
    ]
    assert [l.harness for l in imp.legs[3:6]] == ['opencode', 'opencode', 'opencode']
    for e in CX_EFFORTS:
        if e != 'medium':
            assert f'cx-gpt-6.2-sol-{e}' in EXPLICIT_WRITERS
    assert 'cx-gpt-6.2-sol' in EXPLICIT_WRITERS

    rev = plan_spawn(CARD, role='review', writer_families=('gpt',))
    assert [(l.id, l.effort) for l in rev.legs] == [
        ('agy-gemini-4-argon-medium', 'medium'),
        ('mimo-v2.6-pro', None),
        ('opencode-deepseek-v4.1-flash-free', None),
        ('opencode-go-deepseek-v4-flash', None),
        ('opencode-go-glm-5.3-flash', None),
        ('agy-gemini-4-neon-medium', 'medium'),
        ('claude-sonnet-5-5-high', 'high'),
        ('mimo-v2.6-flash', None),
    ]
    rev_diff = plan_spawn(CARD, role='review', writer_families=('gemini',))
    assert [(l.id, l.effort) for l in rev_diff.legs] == [
        ('mimo-v2.6-pro', None),
        ('opencode-deepseek-v4.1-flash-free', None),
        ('opencode-go-deepseek-v4-flash', None),
        ('opencode-go-glm-5.3-flash', None),
        ('claude-sonnet-5-5-high', 'high'),
        ('mimo-v2.6-flash', None),
        ('cx-gpt-6-luna', None),
        ('agy-gpt-oss-120b', None),
    ]

    # Retired models absent from routes and rejected
    for leg in spawnable_legs():
        assert 'gpt-6.1-sol' not in leg.id and 'gpt-6.1-sol' not in leg.model
        # D-6366: argon is a review leg only; no implement leg serves it.
        assert 'gemini-4-argon' not in leg.model or leg.roles == ('review',)
    assert not any('argon' in l.id for l in imp.legs)
    for bad in ('omniroute/cx-gpt-6.1-sol-xhigh',):
        assert is_retired(bad)
        with pytest.raises(RetiredModelError):
            check_retired(bad)

    # flint-40b-free, big-pickle, beam-free only on low-risk job route, never HIGH review
    low_only = {'opencode-flint-40b-free', 'opencode-big-pickle', 'opencode-go-beam-free'}
    assert low_only.isdisjoint({l.id for l in imp.legs})
    assert low_only.isdisjoint({l.id for l in rev.legs})
    high_rev = plan_spawn({'estimate_tokens': 40000, 'risk': 'high', 'risk_tags': ['auth']},
                          role='review', writer_families=('gpt',))
    assert low_only.isdisjoint({l.id for l in high_rev.legs})
    low_imp = plan_spawn({'estimate_tokens': 40000, 'risk': 'low', 'is_pure_doc': True}, role='implement')
    low_rev = plan_spawn({'estimate_tokens': 40000, 'risk': 'low', 'is_pure_doc': True},
                         role='review', writer_families=('gpt',))
    assert low_only <= {l.id for l in low_imp.legs}
    assert low_only <= {l.id for l in low_rev.legs}

    # Resolver family mapping
    assert detect_model_family('gpt-6.2-sol') == 'gpt' and 'gpt' in MODEL_FAMILIES
    assert detect_model_family('gpt-6.2-astra') == 'gpt'
    assert detect_model_family('gemini-4-neon-medium') == 'gemini' and 'gemini' in MODEL_FAMILIES
