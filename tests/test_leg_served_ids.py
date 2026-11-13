"""Served DeepSeek identities: catalog names never imply host admission."""
import asyncio
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, Leg
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support import resolver


# Measured with `opencode models` on 2026-11-06 (D-3952).
SERVED_OPENCODE_GO_IDS = frozenset({
    "opencode-go/deepseek-v4.1-flash",
    "opencode-go/gemini-4-argon",
    "opencode-go/glm-5.2",
    "opencode-go/qwen3.8-max",
    "opencode-go/muse-spark-1.3-contributor",
})


def test_default_opencode_go_legs_name_served_ids():
    legs = [leg for leg in DEFAULT_LEGS if leg.id.startswith("opencode-go-")]
    assert legs
    for leg in legs:
        assert leg.model in SERVED_OPENCODE_GO_IDS, (leg.id, leg.model)


def test_deepseek_review_registry_model_ids():
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_CATALOG
    expected = {'omniroute-deepseek-flash': 'omniroute/deepseek-v4.1-flash',
                'opencode-go-deepseek-v4-flash': 'opencode-go/deepseek-v4.1-flash'}
    legs = {row['id']: Leg(**row) for row in OPERATOR_ROUTE_CATALOG}
    for id, model in expected.items():
        leg = legs[id]
        assert leg.model == model
        assert leg.roles == ('review',) and leg.harness == 'opencode'
        assert leg.effort == 'medium'


# DeepSeek-only projection of PR #818's recorded standalone listing. The
# corrected OmniRoute id was NOT listed: renaming is not host admission.
@pytest.mark.real_probe
@pytest.mark.parametrize('model,offered', [('omniroute/deepseek-flash', False),
    ('omniroute/deepseek-v4.1-flash', False), ('opencode-go/deepseek-v4.1-flash', True)])
def test_deepseek_recorded_probe_filters_before_launch(monkeypatch, model, offered):
    from plangraph.routing_support import opencode_overlay
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    leg = replace(next(l for l in DEFAULT_LEGS if l.id == 'omniroute-deepseek-flash'), model=model)
    plan = SpawnPlan((leg,), (), 'test', (), ())
    models = frozenset({'console-deepseek/deepseek-flash', 'omniroute/deepseek-direct-flash',
        'opencode-go/deepseek-v4-pro', 'opencode-go/deepseek-v4.1-flash',
        'opencode/deepseek-v4.1-flash', 'opencode/deepseek-v4.1-flash-free'})
    filtered = resolver.plan_for_launch(plan, probe=lambda _: models)
    assert filtered.legs == ((leg,) if offered else ())
    assert filtered.skipped == (() if offered else ({'leg': leg.id,
        'reason': 'not_served_on_host: ' + model,
        'skip_reason': 'not_served_on_host: ' + model},))
    assert plan.legs == (leg,) and not plan.skipped


@pytest.mark.real_probe
def test_unserved_deepseek_never_starts_child(monkeypatch, tmp_path):
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_CATALOG
    from plangraph.routing_support import opencode_overlay
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    leg = next(Leg(**row) for row in OPERATOR_ROUTE_CATALOG if row['id'] == 'omniroute-deepseek-flash')
    plan = SpawnPlan((leg,), (), 'test', (), ())
    monkeypatch.setattr(resolver, 'served_models', lambda *a, **kw: frozenset({
        'opencode-go/deepseek-v4.1-flash'}))
    async def launch(*args, **kwargs):
        pytest.fail('unserved DeepSeek child started')
    service = spawn_service(SpawnerRunner(process_factory=launch), state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='task',
        role='review', writer_families=('gpt',), wait_seconds=None))
    assert not service.runner.jobs
    assert record['status'] == 'failed' and record['chosen_leg'] is None
    assert record['plan']['legs'] == []
    assert record['attempts'][0]['skip_reason'] == 'not_served_on_host: ' + leg.model
