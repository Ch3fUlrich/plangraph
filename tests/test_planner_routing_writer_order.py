"""Writer ordering regression entry points, split from the routing size budget."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest


def test_writer_order_sol_family_excluded():
    from test_route_free_first import test_writer_order_sol_family_excluded as check
    check()


def test_writer_order_privacy_excludes_training_sol():
    from test_route_free_first import test_writer_order_privacy_excludes_training_sol as check
    check()


def test_medium_writer_and_review_defaults():
    from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER
    assert LEAD_WRITER == 'cx-gpt-6.1-sol-medium'
    assert next(l for l in DEFAULT_LEGS if l.id == LEAD_WRITER).effort == 'medium'
    assert next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6-luna-medium').effort == 'medium'
    assert all(l.effort not in ('high', 'xhigh', 'max')
               for l in DEFAULT_LEGS if 'review' in l.roles)


def test_higher_effort_requires_card_and_decision():
    import pytest
    from plangraph.spawner.target import admit_effort
    from plangraph.routing_errors import SpawnRefused
    for card in ({}, {'effort': 'high'}, {'effort_decision': 'D-123'}):
        with pytest.raises(SpawnRefused, match='explicit card effort and D-id'):
            admit_effort('provider/gpt-6.1-sol-high', card, effort='high')
    admit_effort('provider/gpt-6.1-sol-high', {'effort': 'high', 'effort_decision': 'D-123'})
    admit_effort('provider/gpt-6.1-sol-medium', {})


def test_declared_effort_is_used_instead_of_model_suffix():
    from plangraph.spawner.target import admit_effort
    admit_effort('provider/gpt-6.1-sol-high', {}, effort='medium')


@pytest.fixture(params=['base', 'routed'])
def effort_route(request, tmp_path, monkeypatch):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import JobInfo, spawn_service
    from plangraph.spawner.service import SpawnService
    base = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-medium')
    high = replace(base, id='first', model='provider/model-high', effort='high')
    medium = replace(base, id='second', model='provider/muse-ember-max', effort=None)
    calls = []
    runner = SimpleNamespace(jobs={}, _processes={}, _tasks={}, provider_responses={}, default_cwd=str(tmp_path))
    sink = SimpleNamespace(deliver=lambda *a: None)
    service = (SpawnService(runner=runner, state=tmp_path / 'state') if request.param == 'base' else
               spawn_service(runner=runner, state=tmp_path / 'state', completion_sink=sink, keeper_sink=sink))
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a, **k: None)
    monkeypatch.setattr(service, '_annotate_spawn', lambda *a: None)
    monkeypatch.setattr(service, '_prepare_leg_prompt', lambda *a, **k: asyncio.sleep(0, 'work'))
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', lambda p, **k: asyncio.sleep(0, p))
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', lambda *a: asyncio.sleep(0))
    async def launch(record, chosen, **kwargs):
        calls.append(chosen)
        return JobInfo('job-test', 'completed', 'work', chosen.model, exit_code=0, stdout='done')
    monkeypatch.setattr(service, '_launch_leg', launch)
    def run(legs, card=None):
        plan = SpawnPlan(tuple(legs), (), 'test', (), ())
        monkeypatch.setattr(service, '_route', lambda *a, **k: plan)
        return asyncio.run(service.spawn(dict(estimate_tokens=100, **(card or {})), 'work', wait_seconds=None))
    return run, calls, high, medium


def test_disallowed_effort_skips_to_next_leg(effort_route):
    run, calls, high, medium = effort_route
    record = run([high, medium])
    assert record['status'] == 'completed', record
    assert [leg.id for leg in calls] == ['second']
    assert record['attempts'][0]['status'] == 'skipped'
    assert 'effort' in record['attempts'][0]['skip_reason']


def test_max_model_name_is_not_effort(effort_route):
    run, calls, _, medium = effort_route
    assert run([medium])['status'] == 'completed'
    assert calls == [medium]


def test_explicit_high_request_and_decision_admit_high_leg(effort_route):
    run, calls, high, medium = effort_route
    assert run([high, medium], {'effort': 'high', 'effort_decision': 'D-123'})['status'] == 'completed'
    assert calls == [high]


def test_only_disallowed_legs_refuse_with_skip_reason(effort_route):
    run, calls, high, _ = effort_route
    record = run([high])
    assert record['status'] == 'refused'
    assert not calls
    assert record['attempts'][0]['status'] == 'skipped'
    assert 'effort' in record['attempts'][0]['skip_reason']


def test_ember_declaration_does_not_claim_max_reasoning_effort():
    from plangraph.planner.legs import EXPLICIT_LEGS
    from plangraph.spawner.target import admit_effort
    leg = next(l for l in EXPLICIT_LEGS if l.id == 'muse-ember-max')
    assert leg.effort is None
    admit_effort(leg.model, {}, leg.effort)
