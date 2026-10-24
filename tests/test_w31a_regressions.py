import asyncio
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner.runner import JobInfo, spawn_service


def test_failed_status_blocks_continuation(tmp_path):
    service = spawn_service(state=tmp_path)
    record = dict(status='failed', exit_code=0, role='implement')
    asyncio.run(service._continue_writer(record, 'Remaining gaps: fix this'))
    assert record['continuation_reason'] == 'nonzero_exit'
    assert 'continuation_run_id' not in record


@pytest.mark.parametrize('all_removed', [False, True])
def test_host_readmission_after_provider_stop(tmp_path, monkeypatch, all_removed):
    from dataclasses import replace
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import resolver
    a, b, c = [replace(DEFAULT_LEGS[0], id=n, model='test/' + n, harness='opencode')
               for n in ('a', 'b', 'c')]
    calls = []
    async def spawn(**kw):
        calls.append(kw['model_or_combo'])
        first = len(calls) == 1
        return JobInfo('a' if first else 'c', 'failed' if first else 'completed',
                       'x', kw['model_or_combo'], exit_code=1 if first else 0)
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={},
                             provider_responses={'a': {'status': 429}})
    service = spawn_service(state=tmp_path, runner=runner)
    plans = iter([SpawnPlan((a,), (), 'test', (), ()),
                  SpawnPlan((b,) if all_removed else (b, c), (), 'test', (), ())])
    monkeypatch.setattr(service, '_route', lambda *args, **kw: next(plans))
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda row, **kw:
                       'not_served_on_host: ' + row['model'] if row['model'] == b.model else None)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert calls == ([a.model] if all_removed else [a.model, c.model])
    assert result['status'] == ('failed' if all_removed else 'completed')
    assert any('not_served_on_host' in (a['skip_reason'] or '') for a in result['attempts'])


@pytest.mark.parametrize('host', [True, False])
def test_all_legs_removed_classification(tmp_path, monkeypatch, host):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import resolver
    reason = 'not_served_on_host: x' if host else 'limited'
    plan = SpawnPlan((), ({'leg': 'x', 'reason': reason, 'skip_reason': reason},), 'test', (), ())
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert result['status'] == ('failed' if host else 'limited')
    assert result['exit_code'] == (1 if host else 4)
    if host:
        assert reason in result['error']


@pytest.mark.parametrize('rc,text,expected', [
    (0, "Done: added handling for 'unknown model' errors", 'completed'),
    (1, 'Error: Model unavailable: x', 'failed'),
    (1, '', 'failed'),
])
def test_attempt_outcome(tmp_path, monkeypatch, rc, text, expected):
    leg = DEFAULT_LEGS[0]
    job = JobInfo('j', 'completed' if rc == 0 else 'failed', 'x', leg.model,
                  exit_code=rc, stdout=text)
    async def spawn(**kw):
        return job
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(state=tmp_path, runner=runner)
    plan = SimpleNamespace(legs=[leg], error=None, to_dict=lambda: {'skipped': []})
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert result['status'] == expected
    assert len(result['attempts']) == 1
    if rc:
        assert result['error']
        assert result['attempts'][0]['skip_reason']


def test_env_redaction_before_bounds(tmp_path, monkeypatch):
    secret = 'opaque' '-credential-value'
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', secret)
    service = spawn_service(state=tmp_path)
    record = dict(run_id='run-test', status='failed', exit_code=0,
                  error=secret + 'x' * 10_000_000,
                  attempts=[dict(status='failed', skip_reason=secret + 'x' * 10_000_000)])
    service._save(record)
    result = service.result('run-test')
    assert secret not in service._path('run-test').read_text()
    assert len(result['error']) <= 2000
    assert len(result['attempts'][0]['skip_reason']) <= 300
    assert result['exit_code'] == 1


def test_supervisor_secret_straddles_old_boundary(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    from plangraph.spawner import cli
    secret = 'opaque' '-boundary-credential'
    monkeypatch.setenv('AUTOOS_TEST_TOKEN', secret)
    message = 'prefix ' + secret + 'x' * (256 * 1024 - len(secret) // 2)
    def start(command, **kwargs):
        return subprocess.Popen([sys.executable, '-c',
            'import sys; sys.stdin.readline(); sys.stderr.write(' + repr(message) + ')'], **kwargs)
    monkeypatch.setattr(cli, '_start_supervisor', start)
    assert cli.main(['--state', str(tmp_path), 'spawn', 'x', '--card', '{"estimate_tokens":100}',
                     '--leg', 'missing', '--override-reason', 'test']) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert secret[len(secret) // 2:] not in output.err


@pytest.mark.parametrize('rc,stderr,expected', [
    (0, 'Error: Model unavailable: x', 'completed'),
    (1, 'Error: Model unavailable: x', 'failed'),
])
def test_explicit_leg_error_rule(tmp_path, monkeypatch, rc, stderr, expected):
    from plangraph.spawner.service import SpawnService
    leg = DEFAULT_LEGS[0]
    job = JobInfo('j', expected, 'x', leg.model, exit_code=rc, stderr=stderr)
    async def spawn(**kw):
        return job
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = SpawnService(state=tmp_path, runner=runner)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, leg=leg.id,
        override_reason='W31A explicit regression', wait_seconds=None))
    assert result['status'] == expected
    assert len(result['attempts']) == 1
    assert result['attempts'][0]['skip_reason'] == (stderr if rc else None)
