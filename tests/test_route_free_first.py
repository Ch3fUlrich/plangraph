"""D-5212: Sol xhigh then Gemini writers; other legs retain cost-tier ordering."""
from __future__ import annotations

import json
import dataclasses
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")

# Ordering probes include inline-only review legs, which require diff metadata.
CARD = {"estimate_tokens": 20000, "risk_tags": [],
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
FREE = ["opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free", "openrouter-free"]
DISABLED_FREE = {"opencode-deepseek-v4.1-flash-free", "opencode-glm-5.3-flash-free"}
REVIEW_FREE = ["opencode-mimo-v2.6-flash-free", "agy-gemini-3.8-flash", "opencode-big-pickle",
               "opencode-go-longcat-2.5-free", "console-openrouter-free", "opencode-gemini-4-argon"]
# D-4196: both retained Zen rows stay permanently disabled as on main.
ACTIVE_FREE = ["openrouter-free"]
SUBS = ["cx-gpt-6.1-sol-xhigh", "agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash"]
WRITERS = [*SUBS[:2], "openrouter-free", "opencode-big-pickle", "agy-gemini-4-argon",
           "mimo-v2.6-flash", "mimo-v2.6-pro", "omniroute-deepseek-flash",
           "omniroute-muse-spark", "deepseek"]


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [])
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))


def test_main_zen_disables_retained_exactly():
    reason = "D-4196 opencode zen never worked, use opencode go; never remove (D-3828)"
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
    assert next(leg for leg in result.legs if leg.id == "opencode-big-pickle").tier == "free"


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
    assert plan.first_leg.id == SUBS[1]
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
    assert private.first_leg.id == SUBS[1]
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
    assert plan['first_leg']['model'] == 'omniroute/cx-gpt-6.1-sol-xhigh'
    assert plan['first_leg']['effort'] == 'xhigh'
    assert set(plan) == {'legs', 'first_leg', 'skipped', 'p_source', 'review_seats', 'reasons', 'error', 'gaps', 'candidates', 'placement'}
    assert plan['placement'] == {'host': None, 'reason': 'placement_not_evaluated', 'free_mb': None}
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert 'spawn by card' in capsys.readouterr().err


def test_review_mimo_first():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    ids = [leg.id for leg in plan.legs]
    assert ids[:7] == [*REVIEW_FREE, "mimo-v2.6-flash"]
    assert DISABLED_FREE.isdisjoint(ids)
    assert ACTIVE_FREE == [leg.id for leg in DEFAULT_LEGS if leg.id in FREE and not leg.disabled]


def test_policy_banned_free_legs_stay_skipped(tmp_path, monkeypatch):
    path = tmp_path / "route-policy.json"
    path.write_text(json.dumps({"banned_families": ["deepseek"], "off_families": ["glm"]}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    imp = plan_spawn(CARD, role="implement")
    assert [leg.id for leg in imp.legs] == [id for id in WRITERS if id not in
        (FREE[0], "deepseek", "omniroute-deepseek-flash", "opencode-big-pickle")]
    rev = plan_spawn(CARD, role="review", writer_families=("claude",))
    reasons = {s["leg"]: s["reason"] for s in rev.skipped}
    assert reasons[FREE[0]].startswith("disabled: D-4196 ")
    assert reasons[FREE[1]].startswith("disabled: D-4196 ")
    assert reasons["omniroute-deepseek-flash"] == "banned_family"
    assert [leg.id for leg in rev.legs][:7] == [*REVIEW_FREE, "mimo-v2.6-flash"]
    assert "opencode-big-pickle" not in reasons  # D-5743: exact review-only lift.
    reasons = {s["leg"]: s["reason"] for s in rev.skipped}
    assert reasons["opencode-go-deepseek-v4-flash"] == "banned_family"
    assert reasons["opencode-go-glm-5.2"].startswith("disabled:")


def test_agy_skipped_while_out_of_quota():
    def reader(model, **kwargs):
        limited = model in ("gemini-3.8-flash", "agy/gemini-3.8-flash")
        return {"state": "limited" if limited else "available",
                "evidence": {"unavailable_until": 1000} if limited else None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    ids = [leg.id for leg in plan.legs]
    assert ids == [id for id in WRITERS if id != SUBS[1]]
    assert "agy-gemini-3.8-flash" not in ids
    review = plan_spawn(CARD, role="review", writer_families=("gpt",), leg_reader=reader)
    assert {s["leg"]: s["reason"] for s in review.skipped}["agy-gemini-3.8-flash"] == "limited"


def test_cli_route_review_mimo_first(tmp_path, capsys, monkeypatch):
    # Ordering without measurements must not depend on the host's catalog DB.
    from plangraph.routing_support import catalog_db
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", tmp_path / "absent.db")
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "claude", "--json"]) == 0
    ids = [leg["id"] for leg in json.loads(capsys.readouterr().out)["legs"]]
    assert ids[:7] == [*REVIEW_FREE, "mimo-v2.6-flash"]
    assert DISABLED_FREE.isdisjoint(ids)
    assert ids.index("agy-gemini-3.8-flash") == 1


def test_route_sol_unavailable_keeps_tier_writer_order():
    # D-3745: an ineligible lead does not change the fallback ordering.
    def reader(model, **kwargs):
        return {"state": "down" if model == "omniroute/cx-gpt-6.1-sol-xhigh" else "available", "evidence": None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    assert [leg.id for leg in plan.legs] == WRITERS[1:]
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
    assert [leg.id for leg in plan.legs] == ["agy-gemini-3.8-flash",
        "qodercli-qwen3.8-flash", *FREE, "mimo-v2.6-flash", "claude-haiku-5.5", "deepseek"]


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
            table["agy-gemini-3.8-flash"])
    assert [leg.id for leg in route_card(CARD, legs=legs, role="review").legs] == [
        "agy-gemini-3.8-flash", "renamed-review", "second-review"]


@pytest.mark.parametrize("benchmark", [None, {}])
def test_haiku_last_across_tiers_and_measurements(monkeypatch, benchmark):
    from plangraph.planner.route_policy import RoutePolicy
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [])
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = (dataclasses.replace(table["claude-haiku-5.5"], tier="free"),
            dataclasses.replace(table["agy-opus-5.5"], tier="paid"),
            dataclasses.replace(table["agy-gemini-3.8-flash"], tier="paid"))
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
    assert [leg.id for leg in result.legs] == ["claude-haiku-5.5", "agy-opus-5.5", "agy-gemini-3.8-flash"]
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
    for tier in TIERS:
        legs = (replace(table[FREE[2]], disabled=None), replace(table[SUBS[0]], tier=tier),
                replace(table[FREE[0]], disabled=None), table[SUBS[2]])
        # route_card is a low-level ordering probe; plan_spawn filters roles.
        expected = [SUBS[0], FREE[2], FREE[0], SUBS[2]]
        assert [leg.id for leg in route_card(CARD, legs=legs, role="implement").legs] == expected
        fallback = route_card(CARD, legs=legs, role="implement", unavailable=(SUBS[0],))
        assert [leg.id for leg in fallback.legs] == [FREE[2], FREE[0], SUBS[2]]
        for gemini_tier in TIERS:
            writers = (*legs, replace(table[SUBS[1]], tier=gemini_tier))
            assert [leg.id for leg in route_card(CARD, legs=writers, role="implement").legs] == [
                *SUBS[:2], FREE[2], FREE[0], SUBS[2]]
            fallback = route_card(CARD, legs=writers, role="implement", unavailable=(SUBS[0],))
            assert [leg.id for leg in fallback.legs] == [SUBS[1], FREE[2], FREE[0], SUBS[2]]
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
    # D-5212 / rule 3.23a: Sol xhigh, then Gemini, before all other cost tiers.
    # Explicit mechanics table tests all eight ordering slots; production
    # disabled-leg eligibility is independently asserted above and in Go tests.
    c = {"estimate_tokens": 20000, "risk_tags": []}
    expected = [*SUBS[:2], 'opencode-deepseek-v4.1-flash-free',
                'opencode-glm-5.3-flash-free', 'openrouter-free',
                'opencode-big-pickle', 'agy-gemini-4-argon',
                'mimo-v2.6-flash', 'mimo-v2.6-pro', 'omniroute-deepseek-flash',
                'omniroute-muse-spark', 'deepseek']
    for gemini_tier in ("trial", "free", "subscription", "paid"):
        plan = plan_spawn(c, role="implement", catalog_rows=(
            {"id": "agy/gemini-3.8-flash", "tier": gemini_tier},))
        assert [leg.id for leg in plan.legs] == expected


def test_main_policy_gpt_first_and_retained_routes():
    from plangraph.planner.route_policy import load_route_policy, DEFAULT_POLICY_PATH
    policy = load_route_policy(DEFAULT_POLICY_PATH)
    clear = lambda *a, **k: {"state": "ok", "evidence": None}
    implement = plan_spawn(CARD, role="implement", policy=policy, leg_reader=clear)
    assert implement.first_leg.id == SUBS[0]
    assert [leg.id for leg in implement.legs] == [id for id in WRITERS if id not in
        (FREE[0], "deepseek", "omniroute-deepseek-flash", "opencode-big-pickle")]
    for writers in (("mimo",), ("gpt",), ("claude",), ("gpt", "mimo")):
        review = plan_spawn(CARD, role="review", writer_families=writers,
                            policy=policy, leg_reader=clear)
        assert not {leg.family for leg in review.legs} & set(writers)
        assert not {leg.family for leg in review.review_seats} & set(writers)
        # D-5743: free MiMo leads unless fenced by the writer's family.
        assert review.first_leg.id == ("agy-gemini-3.8-flash" if "mimo" in writers else REVIEW_FREE[0])
        assert "deepseek" in {leg.family for leg in review.legs}
        assert [leg.id for leg in review.legs if leg.family == "glm"] == ["opencode-big-pickle"]
    assert {leg.id for leg in DEFAULT_LEGS} == {
        "qodercli-qwen3.8-flash", *FREE, *SUBS, "cx-gpt-6-luna", "agy-sonnet-5.5", "agy-gemini-4-argon", "opencode-big-pickle",
        "agy-opus", "agy-opus-5.5", "omniroute-deepseek-flash", "omniroute-muse-spark", "opencode-gemini-4-argon",
        "opencode-go-gemini-4-argon", "opencode-go-glm-5.2", "opencode-go-qwen3.8-max",
        "omniroute-grok-4.7", "opencode-console-muse-spark",
        "opencode-go-muse-spark", "opencode-go-deepseek-v4-flash", "deepseek", "claude-haiku-5.5", "omniroute-gemini-3.8-flash",
        "agy-gemini-4-argon", "opencode-big-pickle", "agy-claude-opus", *REVIEW_FREE,
        "omniroute-deepseek-flash", "omniroute-muse-spark", "agy-gpt-oss-120b"}
