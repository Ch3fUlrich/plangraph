import asyncio
from dataclasses import replace

import pytest

from plangraph.planner import legs
from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.service import SpawnService


def use_table(monkeypatch, table):
    import plangraph.planner.routing as routing
    original = routing.plan_spawn
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', table)
    monkeypatch.setattr(routing, 'plan_spawn',
        lambda *a, **kw: original(*a, **dict(kw, legs=table)))


def fake_runner(tmp_path, status='completed', stop=None, text='verdict: pass'):
    from types import SimpleNamespace
    calls = []
    async def spawn(**kw):
        calls.append(kw)
        return SimpleNamespace(job_id='job', status=status, exit_code=0 if status == 'completed' else 1,
            stdout=text, stderr=text, served_model='unwitnessed', served_family='unwitnessed',
            served_witness_reason='test', events=[], command=[])
    return SimpleNamespace(spawn=spawn, calls=calls, default_cwd=str(tmp_path),
        _processes={}, _tasks={}, provider_responses={'job': stop} if stop else {})


@pytest.mark.parametrize('harness', ['opencode', 'agy'])
@pytest.mark.parametrize('pin', ['leg', 'model'])
@pytest.mark.parametrize('free_up,reason', [(True, None), (False, None),
    (True, 'D-4382: no free leg witnesses for this task')])
def test_paid_pin(tmp_path, monkeypatch, harness, pin, free_up, reason):
    paid = replace(legs.DEFAULT_LEGS[0], id='paid-test', model='test/paid',
                   tier='paid', harness=harness, disabled=None,
                   roles=('review',), family='paid-family', tool_mode='tools')
    free = replace(paid, id='free-test', model='test/free', tier='free', family='free-family')
    use_table(monkeypatch, (paid, free))
    runner = fake_runner(tmp_path)
    service = SpawnService(runner=runner, state=tmp_path, leg_reader=lambda model, **kw:
        {'state': 'available' if model != free.model or free_up else 'down', 'evidence': None})
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'],
                   override_reason=reason, wait_seconds=None,
                   **{pin: paid.id if pin == 'leg' else paid.model})
    if free_up and reason is None:
        with pytest.raises(SpawnRefused, match='free-test'):
            asyncio.run(service.spawn(**options))
        assert not (tmp_path / 'spawn-overrides.jsonl').exists()
    else:
        # Pin auditing still requires a reason even if no free leg serves.
        options['override_reason'] = reason or 'No eligible free leg is up'
        record = asyncio.run(service.spawn(**options))
        assert runner.calls[0]['client'] == harness
        assert not runner.calls[0]['dry_run']
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
        attempts=[dict(leg={'id': free.id}, status='failed', skip_reason=reason,
                       down_reason='not_served_on_host')]))
    plan = service.route({'estimate_tokens': 100}, role='review', writer_families=['gpt'])
    assert [l.id for l in plan.legs] == ['other', 'free-test']
    from plangraph.planner import measured_down
    evidence = measured_down.measured_down(tmp_path)
    assert evidence[free.id]['reason'] == 'not_served_on_host'
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
    use_table(monkeypatch, (paid, free, up) if one_up else (paid, free))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available'})
    ended = time.time()
    service._save(dict(run_id='run-dead', status='failed', ended=ended,
        attempts=[dict(leg={'id': free.id}, status='failed', down_reason='not_served_on_host')]))
    assert measured_down(tmp_path)[free.id] == {'reason': 'not_served_on_host', 'time': ended}
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'], leg='paid', dry_run=True)
    if one_up:
        with pytest.raises(SpawnRefused) as exc:
            asyncio.run(service.spawn(**options))
        assert 'up-free' in str(exc.value) and 'dead-free' not in str(exc.value)
    else:
        assert asyncio.run(service.spawn(**options))['status'] == 'dry_run'


@pytest.mark.parametrize('status,stop,unavailable,expected', [
    ('failed', {'status': 402}, True, 'credit_limit'),
    ('failed', {'status': 429}, True, 'usage_limit'),
    ('failed', {'status': 402}, False, 'credit_limit'),
    ('failed', {'status': 429}, False, 'usage_limit'),
    ('failed', None, True, 'not_served_on_host'),
    ('failed', None, False, None), ('completed', None, False, None)])
def test_live_evidence(tmp_path, monkeypatch, status, stop, unavailable, expected):
    from plangraph.planner.measured_down import measured_down
    free = replace(legs.DEFAULT_LEGS[1], id='free', model='test/free', family='glm',
                   tier='free', tool_mode='tools', disabled=None, roles=('review',))
    use_table(monkeypatch, (free, replace(free, id='other', model='test/other', family='other')))
    text = 'prompt: usage limit; credit usage limit; not_served_on_host; model not available'
    runner = fake_runner(tmp_path, status, stop, text)
    if unavailable:
        runner = fake_runner(tmp_path, status, stop, 'not_served_on_host')
    service = SpawnService(runner=runner, state=tmp_path,
        leg_reader=lambda *a, **k: {'state': 'available', 'evidence': None})
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt=text,
        role='review', writer_families=['gpt'], wait_seconds=None))
    assert runner.calls and record['host']
    assert record['attempts'][0]['down_reason'] == expected
    assert bool(measured_down(tmp_path)) == bool(expected)
    if expected:
        assert measured_down(tmp_path)[free.id]['reason'] == expected


@pytest.mark.parametrize('free_up', [True, False])
@pytest.mark.parametrize('reason', [' ', 'x', '  sufficiently long reason  '])
def test_reason_normalization(tmp_path, monkeypatch, free_up, reason):
    paid = replace(legs.DEFAULT_LEGS[1], id='paid', model='test/paid', tier='paid',
                   family='glm', tool_mode='tools', disabled=None)
    free = replace(paid, id='free', model='test/free', tier='free')
    use_table(monkeypatch, (paid, free) if free_up else (paid,))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available'})
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'],
                   leg='paid', override_reason=reason, dry_run=True)
    if len(reason.strip()) < 10:
        with pytest.raises(SpawnRefused):
            asyncio.run(service.spawn(**options))
    else:
        assert asyncio.run(service.spawn(**options))['override_reason'] == reason.strip()


def test_record_window_and_corruption(tmp_path, monkeypatch):
    import os
    import time
    from pathlib import Path
    from plangraph.planner.measured_down import LEG_DOWN_WINDOW_S
    free = replace(legs.DEFAULT_LEGS[1], id='free', model='test/free', family='glm',
                   tier='free', tool_mode='tools', disabled=None)
    paid = replace(free, id='paid', model='test/paid', tier='paid', family='other')
    use_table(monkeypatch, (free, paid))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available'})
    now = time.time()
    for i in range(3000):
        path = service._path('run-' + str(i))
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        os.utime(path, (now - LEG_DOWN_WINDOW_S - 10,) * 2)
    for name, text in [('broken', '{'), ('nested', '[' * 10000 + ']' * 10000),
                       ('nohost', '{"ended": %s, "attempts": [{"leg":"free", "status":"failed", "down_reason":"usage_limit"}]}' % now)]:
        path = service._path('run-' + name)
        path.parent.mkdir()
        path.write_text(text)
    opened = []
    original = Path.open
    def counted(path, *a, **kw):
        if path.name == 'record.json':
            opened.append(path)
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, 'open', counted)
    assert service.route({'estimate_tokens': 100}, role='review', writer_families=['gpt']).first_leg == free
    assert len(opened) == 3
    with pytest.raises(SpawnRefused, match='paid pin refused; free legs up: free; --override-reason required'):
        asyncio.run(service.spawn(card={'estimate_tokens': 100}, role='review',
            writer_families=['gpt'], leg=paid.id, override_reason='x', dry_run=True))


@pytest.mark.parametrize('status', ['failed', 'skipped', 'completed'])
def test_saved_structured_skip(tmp_path, status):
    import time
    from plangraph.planner.measured_down import measured_down
    service = SpawnService(state=tmp_path)
    record = dict(run_id='run-skip', ended=time.time(), status='failed',
        plan={'skipped': [{'leg': 'free', 'reason': 'not_served_on_host'}]})
    service._save(record)
    saved = service.result(record['run_id'])
    assert saved['host'] and measured_down(tmp_path)['free']['reason'] == 'not_served_on_host'
    saved['attempts'][0]['status'] = status
    service._save(saved)
    assert bool(measured_down(tmp_path)) == (status != 'completed')


def test_paid_pin_uses_route_exclusions(tmp_path, monkeypatch):
    paid = replace(legs.DEFAULT_LEGS[1], id='paid', model='test/paid', tier='paid',
                   family='glm', tool_mode='tools', disabled=None)
    excluded = replace(paid, id='excluded-free', model='test/free', tier='free', family='gpt')
    use_table(monkeypatch, (paid, excluded))
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **k: {'state': 'available', 'evidence': None})
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, role='review',
        writer_families=['gpt'], leg='paid', dry_run=True))
    assert record['override_reason'] == 'No eligible free leg is up on this host'
