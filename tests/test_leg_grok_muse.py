"""Manual-only Grok and D-5113 automatically admitted Muse contributor seats."""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
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
    # Historical test name retained: D-5113 lifts the old manual-only fence.
    legs = tuple(replace(BY_ID[mid], roles=(role,)) for mid in (*MUSE, 'omniroute-muse-spark'))
    policy = RoutePolicy(review_allowed_families=("muse",))
    for leg in legs:
        assert policy.skip_reason(leg, role) is None
    plan = plan_spawn(CARD, role=role, writer_families=("gpt",), legs=legs, policy=policy)
    assert set(plan.legs) == set(legs)
    denied = plan_spawn(CARD, role=role, writer_families=("gpt",), legs=legs,
                        policy=RoutePolicy(banned_families=("muse",)))
    assert not denied.legs


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
