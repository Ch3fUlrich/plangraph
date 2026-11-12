"""Manual-only Grok and D-5113 automatically admitted Muse contributor seats."""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, MUSE_AMBER, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import SpawnRefused, next_after_failure, plan_spawn, route_card
from plangraph.planner.seats import seat_family
from plangraph.routing_support.opencode_render import render_gateway_providers
from plangraph.routing_support.resolver import host_skip_reason, _probe_served_models
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict  # noqa: E402

GROK = "omniroute-grok-4.7"
MUSE = ("opencode-go-muse-spark", "opencode-console-muse-spark")
CARD = {"estimate_tokens": 20000}
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}


def test_writer_cost_contract_cannot_be_reclassified_by_catalog():
    # Catalog costs cannot relabel the subscription writer or paid Muse as free.
    rows = [dict(id=leg.model if '/' in leg.model else 'agy/' + leg.model,
                 tier='free', available=1, active=True)
            for leg in DEFAULT_LEGS if leg.id in WRITER_ORDER]
    plan = plan_spawn(CARD, role='implement', catalog_rows=rows)
    assert tuple(leg.id for leg in plan.legs) == WRITER_ORDER
    assert tuple(leg.tier for leg in plan.legs) == ('subscription', 'paid')
    assert 'implement' not in BY_ID['agy-gemini-3.8-flash'].roles


def test_catalog_resolvers_share_derivation_and_family_convention():
    from plangraph.routing_support import resolver as shared
    from plangraph.spawner import resolver as compatibility
    assert compatibility is shared
    rows = {row["id"]: row for row in shared.DEFAULT_FALLBACK_MODELS}
    for mid in (GROK, *MUSE):
        assert rows[BY_ID[mid].model]["family"] == BY_ID[mid].family


def test_grok_review_only_and_distinct_family():
    leg = BY_ID[GROK]
    assert (leg.model, leg.family, leg.roles, leg.trains_on_data) == (
        "omniroute/gc/grok-4.7", "grok", ("review",), False)
    assert (leg.harness, leg.tier, leg.tool_mode) == ("opencode", "subscription", "tools")
    assert seat_family(leg.family) == seat_verdict.model_family(leg.model) == "xai"
    assert seat_family("muse") == "meta"
    assert seat_family(leg.family) != seat_family("gpt")
    assert GROK not in {l.id for l in plan_spawn(CARD, role="review", writer_families=("gpt",)).legs}
    assert GROK not in {l.id for l in plan_spawn(CARD, role="implement").legs}


def test_console_muse_uses_served_provider():
    assert BY_ID[MUSE[1]].model == "console-meta/muse-spark-1.3-contributor"
    assert BY_ID[MUSE[0]].model == "opencode-go/muse-spark-1.3-contributor"


def test_console_muse_render_and_probe(monkeypatch, hermetic_spawner_host):
    import os
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "host_probe_env", lambda env: dict(env))
    model = "console-meta/muse-spark-1.3-contributor"
    env = {"PLANGRAPH_GATEWAY_URL": os.environ["PLANGRAPH_GATEWAY_URL"],
           "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {}})}
    assert render_gateway_providers(env, model) is None
    providers = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    assert "console-meta" not in providers
    assert "omniroute" not in providers
    row = dict(harness="opencode", model=model)
    assert host_skip_reason(row, env=env, probe=lambda _: {model}) is None
    assert host_skip_reason(row, env=env, probe=lambda _: set()).startswith("not_served_on_host")
    assert host_skip_reason(row, env={"OPENCODE_CONFIG_CONTENT": json.dumps({
        "providers": {"console-meta": {"env": ["AUTOOS_OMNIROUTE_KEY"]}}})},
        probe=lambda _: {model}).startswith("not_served_on_host")
    other = dict(harness="opencode", model="console-meta/unregistered-model")
    assert host_skip_reason(other, env=env, probe=lambda _: {model}).startswith("not_served_on_host")
    untouched = {"PLANGRAPH_GATEWAY_URL": os.environ["PLANGRAPH_GATEWAY_URL"]}
    assert render_gateway_providers(untouched, BY_ID[GROK].model) is None
    assert "console-meta" not in json.loads(untouched["OPENCODE_CONFIG_CONTENT"])["providers"]


def test_listing_accepts_nested_ids_not_non_ids(monkeypatch):
    from types import SimpleNamespace
    from plangraph.routing_support import resolver
    listing = "\n".join(("omniroute/gc/grok-4.7", "opencode-go/sample",
                         "omniroute/gc/grok-4.8", "console-meta/contributors/muse/spark",
                         "other/nested/id", "omniroute/gc/unknown",
                         "Models available", "bad/", "/bad", "bad//id", "bad/id extra",
                         "bad/nested/", "bad//nested/id", "bad/nested/id extra"))
    def run(args, **kwargs):
        assert args[1:] == ["models", "--standalone"]
        assert kwargs["env"]["OPENCODE_CONFIG_CONTENT"] == "{}"
        assert kwargs["timeout"] == 10
        return SimpleNamespace(returncode=0, stdout=listing)
    monkeypatch.setattr(resolver.subprocess, "run", run)
    assert _probe_served_models("{}", env={"OPENCODE_CONFIG_CONTENT": "{}"}) == frozenset({
        "omniroute/gc/grok-4.7", "opencode-go/sample",
        "omniroute/gc/grok-4.8", "console-meta/contributors/muse/spark",
        "other/nested/id", "omniroute/gc/unknown"})


@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
def test_muse_never_automatic_even_if_lifted(role):
    """Historical name retained: D-5113 admits Muse unless policy bans it."""
    legs = tuple(replace(BY_ID[mid], roles=(role,)) for mid in (*MUSE, 'omniroute-muse-spark'))
    policy = RoutePolicy(review_allowed_families=("muse",))
    for leg in legs:
        assert policy.skip_reason(leg, role) is None
    plan = plan_spawn(CARD, role=role, writer_families=("gpt",), legs=legs, policy=policy,
                      catalog_rows=())
    assert set(plan.legs) == set(legs)
    denied = plan_spawn(CARD, role=role, writer_families=("gpt",), legs=legs,
                        policy=RoutePolicy(banned_families=("muse",)), catalog_rows=())
    assert not denied.legs


@pytest.mark.parametrize("estimate_tokens,paths", [
    (1000, ["docs/guide.md"]),
    (20000, ["src/public_api.py"]),
    (60000, ["tests/test_public_api.py"]),
])
def test_muse_amber_routed_after_sol_and_mimo_public_only(estimate_tokens, paths):
    assert WRITER_ORDER == ("cx-gpt-6.1-sol", MUSE_AMBER) and BY_ID[MUSE_AMBER].trains_on_data
    assert RoutePolicy().skip_reason(BY_ID[MUSE_AMBER], "implement") is None
    assert [l.id for l in plan_spawn(CARD, role="implement").legs] == list(WRITER_ORDER)
    diff = {"repo": "/repo", "base": "HEAD~1", "head": "HEAD"}
    public = dict(estimate_tokens=estimate_tokens, paths=paths, risk_tags=[], diff_ref=diff)
    plan = plan_spawn(public, role="review", writer_families=("gpt",))
    ids = [l.id for l in plan.legs]
    assert "mimo-v2.6-pro" in ids, f"public review must retain mimo-pro: {plan.skipped}"
    assert MUSE_AMBER in ids, f"public review must retain muse-amber: {plan.skipped}"
    positions = {leg_id: i for i, leg_id in enumerate(ids)}
    assert positions["mimo-v2.6-pro"] < positions[MUSE_AMBER], "subscription mimo must precede paid muse-amber"
    assert BY_ID["mimo-v2.6-pro"].tier == "subscription"
    assert BY_ID[MUSE_AMBER].tier == "paid", "muse-amber is pay-as-you-go, not subscription"
    # D-4280 data fence: a risk-tagged card never reaches the training Muse leg.
    for role in ("implement", "review"):
        private = plan_spawn(dict(CARD, risk_tags=["secrets"]), role=role, writer_families=("gpt",))
        assert MUSE_AMBER not in [l.id for l in (*private.legs, *private.review_seats)]
        assert {s["leg"]: s["reason"] for s in private.skipped}[MUSE_AMBER] == "trains_on_data"


def test_muse_amber_catalog_matches_leg():
    from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS
    leg = BY_ID[MUSE_AMBER]
    row = next(row for row in DEFAULT_FALLBACK_MODELS if row["id"] == leg.model)
    assert row.get("trains_on_prompts") == int(leg.trains_on_data) == 1
    assert row["tier"] == leg.tier == "paid"
    assert (row["family"], row["harness"], row["tool_mode"], row["context_window"]) == (
        leg.family, leg.harness, leg.tool_mode, leg.max_request_tokens)


@pytest.mark.parametrize("sol_up", [True, False])
def test_implement_subscriptions_precede_muse_amber(sol_up):
    def reader(model, **kwargs):
        return {"state": "available" if sol_up or model != BY_ID["cx-gpt-6.1-sol"].model else "down",
                "evidence": None}
    plan = plan_spawn(CARD, role="implement", leg_reader=reader)
    expected = (["cx-gpt-6.1-sol"] if sol_up else []) + [MUSE_AMBER]
    assert [leg.id for leg in plan.legs] == expected
    assert BY_ID["cx-gpt-6.1-sol"].model == "omniroute/cx-gpt-6.1-sol"
    assert [leg.tier for leg in plan.legs[:-1]] == ["subscription"] * (len(expected) - 1)
    # Even a reordered table cannot put a paid writer before an eligible subscription.
    shuffled = tuple(BY_ID[mid] for mid in (MUSE_AMBER, "cx-gpt-6.1-sol"))
    assert [leg.id for leg in plan_spawn(CARD, role="implement", legs=shuffled, leg_reader=reader).legs] == expected


def test_implement_no_subscription_writer_uses_muse_before_paid_deepseek():
    # A mechanics-only paid DeepSeek writer; D-3828 still disables it in production.
    deepseek = replace(BY_ID["opencode-go-deepseek-v4-flash"], tier="paid", roles=("implement",))
    writers = tuple(leg for leg in DEFAULT_LEGS if "implement" in leg.roles) + (deepseek,)
    subscriptions = {leg.model for leg in writers if leg.tier == "subscription"}
    def reader(model, **kwargs):
        return {"state": "down" if model in subscriptions or model == "agy/gemini-3.8-flash" else "available",
                "evidence": None}
    plan = plan_spawn(CARD, role="implement", legs=writers, leg_reader=reader, policy=RoutePolicy(),
                      catalog_rows=[dict(id=deepseek.model, tier='paid', available=1, active=True)])
    assert [leg.id for leg in plan.legs] == [MUSE_AMBER, deepseek.id]
    assert {skip["leg"] for skip in plan.skipped} == {"cx-gpt-6.1-sol"}
    assert all(skip["reason"] == "down" for skip in plan.skipped)
    assert all(leg.tier == "paid" for leg in plan.legs)
    production = plan_spawn(CARD, role="implement", leg_reader=reader)
    assert [leg.id for leg in production.legs] == [MUSE_AMBER]
    assert "implement" not in BY_ID[deepseek.id].roles  # D-3828: no production writer lift.


@pytest.mark.parametrize("role", ["implement", "review"])
@pytest.mark.parametrize("exit_class", ["check_fail", "stall", "timeout", "rebase_conflict", "worker_crash"])
@pytest.mark.parametrize("unavailable", [(), ("cx-gpt-6.1-sol",), ("cx-gpt-6-luna",), ("cx-gpt-6.1-sol", "cx-gpt-6-luna")])
@pytest.mark.parametrize("implicit_first", [False, True])
def test_failure_promotion_exhausts_subscriptions_before_muse(role, exit_class, unavailable, implicit_first):
    writers = tuple(replace(BY_ID[mid], roles=(role,)) for mid in
                    (MUSE_AMBER, 'cx-gpt-6.1-sol', 'cx-gpt-6-luna'))
    route = route_card(CARD, legs=writers, role=role, unavailable=unavailable)
    subscriptions = [leg.id for leg in route.legs if leg.tier == "subscription"]
    assert [leg.id for leg in route.legs] == [*subscriptions, MUSE_AMBER]
    tried = [] if implicit_first else [route.first_leg.id] * route.attempt_budget
    for subscription in subscriptions[1:]:
        action, leg = next_after_failure(route, tried, exit_class, route.attempt_budget)
        assert (action, leg.id) == ("rotate", subscription)
        tried = [route.first_leg.id, *tried, leg.id]
    action, leg = next_after_failure(route, tried, exit_class, route.attempt_budget)
    if subscriptions:
        assert (action, leg.id) == ("promote", MUSE_AMBER)
    else:
        assert (action, leg) == ("park", None)
    assert next_after_failure(route, [*subscriptions, MUSE_AMBER], exit_class, route.attempt_budget) == ("park", None)


@pytest.mark.parametrize("exit_class", ["usage_limit", "outage"])
def test_provider_failure_rotates_subscription_writer_before_waiting(exit_class):
    subscriptions = ('cx-gpt-6.1-sol', 'cx-gpt-6-luna')
    route = route_card(CARD, legs=tuple(BY_ID[mid] for mid in (*subscriptions, MUSE_AMBER)), role="implement")
    for tried in ([], [subscriptions[0]]):
        action, leg = next_after_failure(route, tried, exit_class, route.attempt_budget)
        assert (action, leg.id) == ("rotate", subscriptions[1])
    assert next_after_failure(route, subscriptions, exit_class, route.attempt_budget) == ("wait", None)


@pytest.mark.parametrize("status", [402, 429])
@pytest.mark.parametrize("gemini_down", [None, "gemini-3.8-flash", "agy/gemini-3.8-flash"])
def test_provider_stop_exhausts_available_subscription_writers(tmp_path, status, gemini_down):
    from plangraph.planner.routing import next_leg_after_provider_stop
    from plangraph.routing_support.leg_state import LegStateStore
    store = LegStateStore(tmp_path / "legs.sqlite")
    # An explicit mechanics table tests bare/qualified agy limits without enabling
    # the production free, review-only Gemini writer.
    gemini = replace(BY_ID['agy-gemini-3.8-flash'], tier='subscription', roles=('implement',))
    writers = (BY_ID['cx-gpt-6.1-sol'], gemini, BY_ID[MUSE_AMBER])
    if gemini_down:
        store.record_provider_response(gemini_down, status, "quota exhausted", {}, "test", now=100)
    plan = plan_spawn(CARD, role="implement", legs=writers, leg_reader=store, now=100,
        catalog_rows=[dict(id='agy/' + gemini.model, tier='subscription', available=1, active=True)])
    expected = ['cx-gpt-6.1-sol', *([] if gemini_down else [gemini.id]), MUSE_AMBER]
    assert [leg.id for leg in plan.legs] == expected
    for current, following in zip(expected, expected[1:]):
        leg = next_leg_after_provider_stop(plan, current, status, "quota exhausted", {}, "test", store=store, now=100)
        assert leg.id == following
    assert next_leg_after_provider_stop(
        plan, MUSE_AMBER, status, "quota exhausted", {}, "test", store=store, now=100) is None


@pytest.mark.parametrize('measured', [False, True])
def test_review_subscriptions_precede_muse_then_paid_deepseek(measured):
    deepseek = replace(BY_ID["opencode-go-deepseek-v4-flash"], tier="paid")
    # Paid Muse is deliberately before the subscriptions in this input table:
    # D-5240 requires tier ordering, not a hand-set list of subscription ids.
    legs = (BY_ID[MUSE_AMBER], deepseek, BY_ID["agy-gemini-4-argon"], BY_ID["claude-haiku-5.5"])
    benchmark = {deepseek.model: dict(aa_intelligence=999, cost_per_task_usd=0,
                                     pool_burn_weight=0)} if measured else None
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                      leg_reader=lambda *a, **kw: {"state": "available", "evidence": None}, benchmark=benchmark,
                      catalog_rows=[dict(id=deepseek.model, tier='paid', available=1, active=True)])
    assert [leg.id for leg in plan.legs] == ["agy-gemini-4-argon", "claude-haiku-5.5", MUSE_AMBER, deepseek.id]
    assert [leg.tier for leg in plan.legs] == ["subscription", "subscription", "paid", "paid"]
    assert [leg.id for leg in plan.review_seats] == ["agy-gemini-4-argon", "claude-haiku-5.5"]


@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
def test_grok_never_automatic_even_if_lifted(role):
    leg = replace(BY_ID[GROK], roles=(role,))
    policy = RoutePolicy(review_allowed_families=("grok",))
    assert policy.skip_reason(leg, role) == "off_family"
    for spelling in ("xai", "grok-4.7"):
        assert policy.skip_reason(replace(leg, family=spelling), role) == "off_family"
    plan = plan_spawn(CARD, role=role, writer_families=("gpt",), policy=policy)
    assert not any(l.family == "grok" for l in (*plan.legs, *plan.review_seats))


def test_explicit_grok_requires_reason_and_still_works(tmp_path):
    svc = SpawnService(SpawnerRunner(), state=tmp_path)
    kwargs = dict(card=CARD, prompt="Review public material", role="review",
                  writer_families=("gpt",), leg=GROK, dry_run=True)
    with pytest.raises(SpawnRefused, match="override"):
        asyncio.run(svc.spawn(**kwargs))
    result = asyncio.run(svc.spawn(**kwargs, override_reason="Audited Grok review"))
    assert result["chosen_leg"]["id"] == GROK
    assert "Audited Grok review" in (tmp_path / "spawn-overrides.jsonl").read_text()


@pytest.mark.parametrize("family", ["meta", "llama", "llama-4"])
@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
def test_muse_fence_does_not_ban_other_meta_families(family, role):
    leg = replace(BY_ID[MUSE[0]], family=family, roles=(role,))
    assert seat_family(family) == "meta"
    assert RoutePolicy().skip_reason(leg, role) is None


@pytest.mark.parametrize("mid", MUSE)
def test_explicit_muse_requires_reason_and_still_works(tmp_path, mid):
    svc = SpawnService(SpawnerRunner(), state=tmp_path)
    kwargs = dict(card=CARD, prompt="Review public material", role="review",
                  writer_families=("gpt",), leg=mid, dry_run=True)
    with pytest.raises(SpawnRefused, match="override"):
        asyncio.run(svc.spawn(**kwargs))
    result = asyncio.run(svc.spawn(**kwargs, override_reason="Public material only"))
    assert result["chosen_leg"]["id"] == mid
    assert seat_verdict.model_family(BY_ID[mid].model) == "meta"
    assert "Public material only" in (tmp_path / "spawn-overrides.jsonl").read_text()


def test_grok_render_is_scoped_and_preserves_host_refusals(monkeypatch, hermetic_spawner_host):
    # Use the hermetic gateway setting, without embedding any site configuration.
    import os
    gateway = os.environ.get("PLANGRAPH_GATEWAY_URL")
    assert gateway, "hermetic_spawner_host supplies the gateway setting"
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "host_probe_env", lambda env: dict(env))
    env = {"PLANGRAPH_GATEWAY_URL": gateway}
    assert render_gateway_providers(env, BY_ID[GROK].model) is None
    assert "omniroute" not in json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]

    row = dict(harness="opencode", model=BY_ID[GROK].model)
    assert host_skip_reason(row, env=env, probe=lambda _: {row["model"]}) is None
    assert host_skip_reason(row, env=env, probe=lambda _: set()).startswith("not_served_on_host")
    assert host_skip_reason(row, env={}, probe=lambda _: {row["model"]}).startswith("not_served_on_host")
    other = dict(harness="opencode", model="omniroute/unregistered-model")
    assert host_skip_reason(other, env=env, probe=lambda _: {row["model"]}).startswith("not_served_on_host")
    untouched = {"PLANGRAPH_GATEWAY_URL": gateway}
    assert render_gateway_providers(untouched, BY_ID["cx-gpt-6.1-sol"].model) is None
    assert "omniroute" not in json.loads(untouched["OPENCODE_CONFIG_CONTENT"])["providers"]


def test_console_without_gateway_key_is_not_rendered():
    # No gateway URL or pre-render is required for this non-gateway provider.
    env = {}
    assert render_gateway_providers(env, BY_ID[MUSE[1]].model) is None
    assert env == {}


def test_gateway_render_does_not_enroll_console_provider():
    original = {"models": {"muse-spark-1.3-contributor": {"name": "custom"}}}
    env = {"PLANGRAPH_GATEWAY_URL": "https://gateway.example/v1",
           "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {
               "omniroute": {}, "console-meta": original}})}
    assert render_gateway_providers(env, BY_ID[GROK].model) is None
    providers = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    assert providers["console-meta"] == original
    assert providers["omniroute"]["settings"]["baseURL"] == "https://gateway.example/v1"


@pytest.mark.parametrize("mid", (GROK, MUSE[1]))
def test_host_admission_without_pre_render(mid, monkeypatch, hermetic_spawner_host):
    import os
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "host_probe_env", lambda env: dict(env))
    env = {"PLANGRAPH_GATEWAY_URL": os.environ["PLANGRAPH_GATEWAY_URL"]}
    assert "OPENCODE_CONFIG_CONTENT" not in env
    row = dict(harness="opencode", model=BY_ID[mid].model)
    assert host_skip_reason(row, env=env, probe=lambda _: {row["model"]}) is None
    assert host_skip_reason(row, env=env, probe=lambda _: set()).startswith("not_served_on_host")


def test_render_preserves_existing_and_rejects_non_catalog(hermetic_spawner_host):
    import os
    base = {"PLANGRAPH_GATEWAY_URL": os.environ["PLANGRAPH_GATEWAY_URL"]}
    original = {"providers": {"omniroute": {"models": {"gc/grok-4.7": {"name": "custom"}}}}}
    env = dict(base, OPENCODE_CONFIG_CONTENT=json.dumps(original))
    assert render_gateway_providers(env, BY_ID[GROK].model) is None
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]["omniroute"]["models"] == original["providers"]["omniroute"]["models"]
    env = dict(base)
    assert render_gateway_providers(env, "omniroute/unregistered-model") is None
    assert "omniroute" not in json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
