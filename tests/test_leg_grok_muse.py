"""Manual-only Grok and D-4280 Muse contributor seats."""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, MUSE_AMBER, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.planner.seats import seat_family
from plangraph.routing_support.opencode_render import render_gateway_providers
from plangraph.routing_support.resolver import host_skip_reason, _probe_served_models
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict  # noqa: E402

GROK = "omniroute-grok-4.7"
MUSE = ("opencode-go-muse-spark", "opencode-console-muse-spark")
MUSE_MANUAL = ("opencode-go-muse-spark",)  # D-5173: the console leg is routed
CARD = {"estimate_tokens": 20000}
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}


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
    legs = tuple(replace(BY_ID[mid], roles=(role,)) for mid in MUSE_MANUAL)
    policy = RoutePolicy(review_allowed_families=("muse",))
    for leg in legs:
        assert policy.skip_reason(leg, role) == "off_family"
    plan = plan_spawn(CARD, role=role, writer_families=("gpt",), policy=policy)
    assert {l.id for l in (*plan.legs, *plan.review_seats) if l.family == "muse"} <= {MUSE_AMBER}


@pytest.mark.parametrize("estimate_tokens,paths", [
    (1000, ["docs/guide.md"]),
    (20000, ["src/public_api.py"]),
    (60000, ["tests/test_public_api.py"]),
])
def test_muse_amber_routed_after_sol_and_mimo_public_only(estimate_tokens, paths):
    # D-5240: subscription Gemini writes before paid Muse, even when Sol is down.
    assert WRITER_ORDER == ("cx-gpt-6.1-sol", "agy-gemini-3.8-flash", MUSE_AMBER) and BY_ID[MUSE_AMBER].trains_on_data
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
    expected = (["cx-gpt-6.1-sol"] if sol_up else []) + ["agy-gemini-3.8-flash", MUSE_AMBER]
    assert [leg.id for leg in plan.legs] == expected
    assert BY_ID["cx-gpt-6.1-sol"].model == "omniroute/cx-gpt-6.1-sol-xhigh"
    assert [leg.tier for leg in plan.legs[:-1]] == ["subscription"] * (len(expected) - 1)
    # Even a reordered table cannot put a paid writer before an eligible subscription.
    shuffled = tuple(BY_ID[mid] for mid in (MUSE_AMBER, "agy-gemini-3.8-flash", "cx-gpt-6.1-sol"))
    assert [leg.id for leg in plan_spawn(CARD, role="implement", legs=shuffled, leg_reader=reader).legs] == expected


def test_implement_no_subscription_writer_uses_muse_before_paid_deepseek():
    # A mechanics-only paid DeepSeek writer; D-3828 still disables it in production.
    deepseek = replace(BY_ID["opencode-go-deepseek-v4-flash"], tier="paid", roles=("implement",))
    writers = tuple(leg for leg in DEFAULT_LEGS if "implement" in leg.roles) + (deepseek,)
    subscriptions = {leg.model for leg in writers if leg.tier == "subscription"}
    def reader(model, **kwargs):
        return {"state": "down" if model in subscriptions or model == "agy/gemini-3.8-flash" else "available",
                "evidence": None}
    plan = plan_spawn(CARD, role="implement", legs=writers, leg_reader=reader, policy=RoutePolicy())
    assert [leg.id for leg in plan.legs] == [MUSE_AMBER, deepseek.id]
    assert {skip["leg"] for skip in plan.skipped} == {"cx-gpt-6.1-sol", "agy-gemini-3.8-flash"}
    assert all(skip["reason"] == "down" for skip in plan.skipped)
    assert all(leg.tier == "paid" for leg in plan.legs)
    production = plan_spawn(CARD, role="implement", leg_reader=reader)
    assert [leg.id for leg in production.legs] == [MUSE_AMBER]
    assert "implement" not in BY_ID[deepseek.id].roles  # D-3828: no production writer lift.


def test_review_subscriptions_precede_muse_then_paid_deepseek():
    deepseek = replace(BY_ID["opencode-go-deepseek-v4-flash"], tier="paid")
    # Paid Muse is deliberately before the subscriptions in this input table:
    # D-5240 requires tier ordering, not a hand-set list of subscription ids.
    legs = (BY_ID[MUSE_AMBER], deepseek, BY_ID["agy-gemini-3.8-flash"], BY_ID["claude-haiku-5.5"])
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                      leg_reader=lambda *a, **kw: {"state": "available", "evidence": None})
    assert [leg.id for leg in plan.legs] == ["agy-gemini-3.8-flash", "claude-haiku-5.5", MUSE_AMBER, deepseek.id]
    assert [leg.tier for leg in plan.legs] == ["subscription", "subscription", "paid", "paid"]
    assert [leg.id for leg in plan.review_seats] == ["agy-gemini-3.8-flash", "claude-haiku-5.5"]


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
def test_explicit_muse_requires_reason_and_still_works(tmp_path, mid, monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    svc = SpawnService(SpawnerRunner(), state=tmp_path)
    free = replace(BY_ID["qodercli-qwen3.8-flash"], disabled=None)
    monkeypatch.setattr(svc, "_route", lambda *a, **kw: SpawnPlan((free,), (), "test", (), ()))
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
    # D-5240's Sol xhigh writer needs its scoped model entry, unlike base Sol.
    assert json.loads(untouched["OPENCODE_CONFIG_CONTENT"])["providers"]["omniroute"]["models"] == {
        "cx-gpt-6.1-sol-xhigh": {"name": "cx-gpt-6.1-sol-xhigh", "modelID": "cx/gpt-6.1-sol-xhigh",
                                 "limit": {"context": 400000, "output": 128000}}}


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
