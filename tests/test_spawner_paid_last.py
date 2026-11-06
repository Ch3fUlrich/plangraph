import asyncio
from dataclasses import replace

import pytest

from plangraph.planner import legs
from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('harness', ['opencode', 'agy'])
@pytest.mark.parametrize('pin', ['leg', 'model'])
@pytest.mark.parametrize('free_up,reason', [(True, None), (False, None),
    (True, 'D-4382: no free leg witnesses for this task')])
def test_paid_pin(tmp_path, monkeypatch, harness, pin, free_up, reason):
    paid = replace(legs.DEFAULT_LEGS[0], id='paid-test', model='test/paid',
                   tier='paid', harness=harness, disabled=None,
                   roles=('review',), family='paid-family', tool_mode='tools')
    free = replace(paid, id='free-test', model='test/free', tier='free', family='free-family')
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (paid, free))
    service = SpawnService(state=tmp_path, leg_reader=lambda model, **kw:
        {'state': 'available' if model != free.model or free_up else 'down', 'evidence': None})
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'],
                   override_reason=reason, dry_run=True,
                   **{pin: paid.id if pin == 'leg' else paid.model})
    if free_up and reason is None:
        with pytest.raises(SpawnRefused, match='free-test'):
            asyncio.run(service.spawn(**options))
        assert not (tmp_path / 'spawn-overrides.jsonl').exists()
    else:
        # Pin auditing still requires a reason even if no free leg serves.
        options['override_reason'] = reason or 'No eligible free leg is up'
        record = asyncio.run(service.spawn(**options))
        assert record['status'] == 'dry_run'
        assert service.result(record['run_id'])['override_reason'] == options['override_reason']


def test_only_sol_writes():
    assert legs.WRITER_ORDER == ('cx-gpt-6.1-sol',)
    assert [leg.id for leg in legs.DEFAULT_LEGS if 'implement' in leg.roles] == list(legs.WRITER_ORDER)
    assert {'deepseek', 'openrouter-free', 'mimo-v2.6-pro'} <= {leg.id for leg in legs.DEFAULT_LEGS}


@pytest.mark.parametrize('reason', ['credit usage limit', 'model not available',
    'Free usage limit reached', 'not_served_on_host'])
def test_recorded_down_route(tmp_path, monkeypatch, reason):
    import time
    free = replace(legs.DEFAULT_LEGS[1], id='free-test', tool_mode='tools')
    other = replace(free, id='other', model='test/other', tier='paid', family='glm')
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (free, other))
    import plangraph.planner.routing as routing
    original = routing.plan_spawn
    monkeypatch.setattr(routing, 'plan_spawn', lambda *a, **kw: original(*a, **dict(kw, legs=(free, other))))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available'})
    service._save(dict(run_id='run-evidence', status='failed', ended=time.time(),
        attempts=[dict(leg={'id': free.id}, status='failed', skip_reason=reason)]))
    plan = service.route({'estimate_tokens': 100}, role='review', writer_families=['gpt'])
    assert [l.id for l in plan.legs] == ['other', 'free-test']
    from plangraph.planner import measured_down
    evidence = measured_down.measured_down(tmp_path)
    assert evidence[free.id]['reason'] == reason
    monkeypatch.setattr(measured_down.time, 'time', lambda: evidence[free.id]['time'] + measured_down.LEG_DOWN_WINDOW_S)
    plan = service.route({'estimate_tokens': 100}, role='review', writer_families=['gpt'])
    assert [l.id for l in plan.legs] == ['free-test', 'other']


@pytest.mark.parametrize('mode', ['expired', 'test failure', 'timeout', 'other-host', 'missing', 'corrupt'])
def test_no_cooldown(tmp_path, monkeypatch, mode):
    import json
    from plangraph.planner.measured_down import measured_down, LEG_DOWN_WINDOW_S
    monkeypatch.setenv('PLANGRAPH_HOST', 'here')
    path = tmp_path / 'runs/run-one/record.json'
    path.parent.mkdir(parents=True)
    record = dict(host='elsewhere' if mode == 'other-host' else 'here',
        ended=100, attempts=[dict(leg='free', status='failed',
        skip_reason=mode if mode in ('test failure', 'timeout') else 'credit usage limit')])
    if mode != 'missing':
        path.write_text('broken' if mode == 'corrupt' else json.dumps(record))
    assert measured_down(tmp_path, now=100 + LEG_DOWN_WINDOW_S if mode == 'expired' else 101) == {}


@pytest.mark.parametrize('one_up', [False, True])
def test_paid_pin_measured(tmp_path, monkeypatch, one_up):
    import time
    from plangraph.planner.measured_down import measured_down
    paid = replace(legs.DEFAULT_LEGS[1], id='paid', model='test/paid', tier='paid', family='paid', tool_mode='tools')
    free = replace(paid, id='dead-free', model='test/free', tier='free', family='glm')
    up = replace(free, id='up-free', model='test/up')
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (paid, free, up) if one_up else (paid, free))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available'})
    ended = time.time()
    service._save(dict(run_id='run-dead', status='failed', ended=ended,
        attempts=[dict(leg={'id': free.id}, status='failed', skip_reason='model not available')]))
    assert measured_down(tmp_path)[free.id] == {'reason': 'model not available', 'time': ended}
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'], leg='paid', dry_run=True)
    if one_up:
        with pytest.raises(SpawnRefused) as exc:
            asyncio.run(service.spawn(**options))
        assert 'up-free' in str(exc.value) and 'dead-free' not in str(exc.value)
    else:
        assert asyncio.run(service.spawn(**options))['status'] == 'dry_run'
