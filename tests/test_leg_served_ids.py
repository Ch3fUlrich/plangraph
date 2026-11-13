"""Served DeepSeek identities: catalog names never imply host admission."""
import asyncio
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
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
    expected = {'omniroute-deepseek-flash': 'omniroute/deepseek-flash',
                'opencode-go-deepseek-v4-flash': 'opencode-go/deepseek-v4.1-flash'}
    from plangraph.planner.review_order import payment_order
    legs = {leg.id: leg for leg in payment_order(
        tuple(leg for leg in DEFAULT_LEGS if leg.id in expected), 'review', order_class='standard',
        writer_families=('gpt',))}
    for id, model in expected.items():
        leg = legs[id]
        assert leg.model == model
        assert leg.roles == ('review',)
        assert leg.harness == 'opencode'
        assert leg.effort is None


# DeepSeek-only projection of PR #818's recorded standalone listing. The
# corrected OmniRoute id was NOT listed: renaming is not host admission.
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


@pytest.mark.parametrize('model', ['omniroute/deepseek-flash', 'omniroute/deepseek-v4.1-flash'])
def test_unserved_deepseek_never_starts_child(monkeypatch, tmp_path, model):
    from plangraph.routing_support import opencode_overlay
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    leg = replace(next(leg for leg in DEFAULT_LEGS if leg.id == 'omniroute-deepseek-flash'), model=model)
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


def test_hermetic_deepseek_tests_run_without_real_probe_marker(request):
    marks = getattr(request.module, 'pytestmark', ())
    marks = marks if isinstance(marks, (list, tuple)) else (marks,)
    assert 'real_probe' not in {mark.name for mark in marks}
    for test in (test_deepseek_recorded_probe_filters_before_launch,
                 test_unserved_deepseek_never_starts_child):
        assert 'real_probe' not in {mark.name for mark in getattr(test, 'pytestmark', ())}


@pytest.mark.parametrize('marks', [pytest.mark.real_probe, [pytest.mark.real_probe]])
def test_module_level_real_probe_marker_is_detected(request, monkeypatch, marks):
    monkeypatch.setattr(request.module, 'pytestmark', marks, raising=False)
    with pytest.raises(AssertionError):
        test_hermetic_deepseek_tests_run_without_real_probe_marker(request)


def test_hermetic_deepseek_cases_execute_under_default_selection():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
        __file__ + '::test_deepseek_recorded_probe_filters_before_launch',
        __file__ + '::test_unserved_deepseek_never_starts_child'],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '5 passed' in result.stdout and 'deselected' not in result.stdout


def test_position_8_is_dead_until_host_evidence(monkeypatch):
    from plangraph.routing_support import opencode_overlay
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'omniroute-deepseek-flash')
    # Neither identity is served: report dead rather than invent an admission.
    assert leg.model == 'omniroute/deepseek-flash'
    for model in (leg.model, 'omniroute/deepseek-v4.1-flash'):
        plan = SpawnPlan((replace(leg, model=model),), (), 'test', (), ())
        result = resolver.plan_for_launch(plan, probe=lambda _: frozenset({
            'opencode-go/deepseek-v4.1-flash'}))
        assert not result.legs
        assert result.skipped[0]['reason'] == 'not_served_on_host: ' + model
