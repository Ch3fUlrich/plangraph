"""Placement gaps stay explicit until admission samples host resources."""
import asyncio
from unittest.mock import Mock

import pytest

from plangraph.planner.routing import SpawnPlan, SpawnRefused, plan_spawn
from plangraph.spawner import overflow, seat_spawn
from test_admission_overflow import orchestration


def test_direct_plan_records_placement_gap():
    plan = plan_spawn({'estimate_tokens': 100}, role='implement')
    assert plan.to_dict()['placement'] == dict(
        host=None, reason='placement_not_evaluated', free_mb=None)


@pytest.mark.parametrize('legs', [(), None])
def test_direct_plan_accepts_sampled_placement(legs):
    placement = dict(host='host-b', reason='remote_most_free_ram', free_mb=9000)
    options = {} if legs is None else {'legs': legs}
    plan = plan_spawn({'estimate_tokens': 100}, role='implement',
                      placement=placement, **options)
    assert plan.to_dict()['placement'] == placement


def test_route_does_not_sample_admission(orchestration, monkeypatch):
    service, _, remote, _, _ = orchestration
    monkeypatch.setattr(service, '_route', lambda *args, **kwargs: SpawnPlan((), (), '', (), ()))
    blocked = Mock(side_effect=AssertionError('planning must not perform admission I/O'))
    for name in ('registry_caps', 'ready_records', 'available_memory_mb', 'swap_used_pct'):
        monkeypatch.setattr(overflow, name, blocked)
    assert service.route({}).to_dict()['placement']['reason'] == 'placement_not_evaluated'
    blocked.assert_not_called()
    remote.assert_not_called()


def test_benchmark_preview_names_local_constraint(orchestration):
    service, _, remote, _, _ = orchestration
    placement = seat_spawn.admit(service, {'benchmark': object()}, preview=True)
    assert placement == dict(host='host-a', reason='local_benchmark_database', free_mb=8000)
    options = {'benchmark': object()}
    assert service.admit_spawn(options) is None
    assert options['_placement'] == placement
    remote.assert_not_called()


def test_fallback_refusal_preserves_original_and_placement(orchestration, monkeypatch):
    service, rows, _, _, _ = orchestration
    rows['host-b']['ok'] = False
    original = SpawnRefused('named spawn refusal')

    async def refuse(card=None, prompt='', role='implement', **kwargs):
        raise original

    monkeypatch.setattr(service, '_spawn', refuse)
    with pytest.raises(SpawnRefused) as caught:
        asyncio.run(service.spawn({'estimate_tokens': 100}, 'task', 'review'))
    assert caught.value is original
    records = service.ps()['runs']
    assert len(records) == 1
    record = service.result(records[0]['run_id'])
    assert record['status'] == 'refused' and record['error'] == str(original)
    assert record['role'] == 'review'
    assert record['host'] == record['placement']['host'] == 'host-a'
    assert record['placement'] == dict(host='host-a', reason='local_fallback_no_remote', free_mb=8000)


def test_missing_swap_refuses_only_local_fallback(orchestration, monkeypatch):
    service, rows, remote, _, _ = orchestration
    monkeypatch.setattr(overflow, 'swap_used_pct', Mock(side_effect=ValueError('swap readings missing')))
    record = service.admit_spawn({})
    assert record['placement']['host'] == 'host-b'
    rows['host-b']['ok'] = False
    record = service.admit_spawn({})
    assert record['status'] == 'refused' and record['reason'] == 'local_swap_unavailable'
    assert service.result(record['run_id'])['placement'] == dict(
        host='host-a', reason='local_swap_unavailable', free_mb=8000)
    remote.assert_called_once()


@pytest.mark.parametrize('failure', [False, True])
def test_orchestration_remote_retry_or_private_fallback(orchestration, failure):
    service, _, remote, _, _ = orchestration
    if failure:
        remote.side_effect = overflow.RemoteRefusal('remote command failed')
    options = {}
    record = seat_spawn.admit(service, options, card=None if failure else {'diff_ref': {'repo': '/private'}})
    assert record is None and options['_placement']['reason'] == 'local_fallback_no_remote'
    assert remote.call_count == int(failure)


@pytest.mark.parametrize('override', [False, True])
def test_orchestration_production_local_record(orchestration, monkeypatch, override):
    from registry_route_util import _Runner
    from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER
    from plangraph.spawner.runner import spawn_service
    service, rows, _, path, _ = orchestration
    rows['host-b']['ok'] = False
    runner = _Runner()
    runner.default_cwd = str(path.parent)
    service = spawn_service(runner, state=path.parent / 'dispatch-state')
    lead = next(leg for leg in DEFAULT_LEGS if leg.id == LEAD_WRITER)
    monkeypatch.setattr(service, '_route', lambda *args, **kwargs: SpawnPlan((lead,), (), '', (), ()))
    options = dict(leg=lead.id, override_reason='test placement fallback') if override else {}
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='task',
        wait_seconds=None, **options))
    assert service.result(record['run_id'])['placement'] == dict(
        host='host-a', reason='local_fallback_no_remote', free_mb=8000)
    assert record['plan']['placement'] == record['placement']


def test_research_uses_orchestrate_model_policy():
    card = {'estimate_tokens': 100}
    assert plan_spawn(card, role='research').to_dict() == plan_spawn(card, role='orchestrate').to_dict()
