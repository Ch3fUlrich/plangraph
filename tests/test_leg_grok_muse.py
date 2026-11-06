"""Grok review admission and D-4280 manual-only Muse contributor seats."""
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
from plangraph.routing_support.resolver import host_skip_reason
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict  # noqa: E402

GROK = "omniroute-grok-4.7"
MUSE = ("opencode-go-muse-spark", "opencode-console-muse-spark")
CARD = {"estimate_tokens": 20000}
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}


def test_grok_review_only_and_distinct_family():
    leg = BY_ID[GROK]
    assert (leg.model, leg.family, leg.roles, leg.trains_on_data) == (
        "omniroute/gc/grok-4.7", "grok", ("review",), False)
    assert (leg.harness, leg.tier, leg.tool_mode) == ("opencode", "subscription", "tools")
    assert seat_family(leg.family) == seat_verdict.model_family(leg.model) == "xai"
    assert seat_family("muse") == "meta"
    assert seat_family(leg.family) != seat_family("gpt")
    assert GROK in {l.id for l in plan_spawn(CARD, role="review", writer_families=("gpt",)).legs}
    assert GROK not in {l.id for l in plan_spawn(CARD, role="implement").legs}


def test_console_muse_uses_served_provider():
    assert BY_ID[MUSE[1]].model == "opencode-go/muse-spark-1.3-contributor"


@pytest.mark.real_probe
def test_listing_accepts_nested_ids_not_non_ids(monkeypatch):
    from types import SimpleNamespace
    from plangraph.routing_support import resolver
    listing = "\n".join(("omniroute/gc/grok-4.7", "opencode-go/sample",
                         "Models available", "bad/", "/bad", "bad//id", "bad/id extra"))
    monkeypatch.setattr(resolver.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=listing))
    assert resolver._probe_served_models("{}") == frozenset({
        "omniroute/gc/grok-4.7", "opencode-go/sample"})


@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
def test_muse_never_automatic_even_if_lifted(role):
    legs = tuple(replace(BY_ID[mid], roles=(role,)) for mid in MUSE)
    policy = RoutePolicy(review_allowed_families=("muse",))
    for leg in legs:
        assert policy.skip_reason(leg, role) == "off_family"
    plan = plan_spawn(CARD, role=role, writer_families=("gpt",))
    assert not any(l.family == "muse" for l in (*plan.legs, *plan.review_seats))


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
