import json
import subprocess
import asyncio
from types import SimpleNamespace
import pytest

from plangraph.spawner import resolver


def test_root_overlay(tmp_path, monkeypatch):
    from plangraph.spawner.opencode_overlay import overlay_env
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'private-fixture-value')
    (tmp_path / 'opencode.jsonc').write_text('''{
// comment
"providers": {"omniroute": {"env": ["AUTOOS_OMNIROUTE_KEY"], "settings": {"a": 1,},},},
}''')
    env = overlay_env({'OPENCODE_CONFIG_CONTENT': json.dumps({'other': True, 'providers': {'omniroute': {'settings': {'b': 2}}}})})
    cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert cfg['providers']['omniroute']['settings'] == {'a': 1, 'b': 2}
    assert cfg['other'] is True
    assert env['AUTOOS_OMNIROUTE_KEY'] == 'private-fixture-value'
    assert 'private-fixture-value' not in env['OPENCODE_CONFIG_CONTENT']


def test_absent_overlay(tmp_path, monkeypatch):
    from plangraph.spawner.opencode_overlay import overlay_env
    from plangraph.spawner.opencode_overlay import OVERLAY_ROOT_MISSING
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    # A missing root adds only the marker naming the missing file (SPAWN-OVERLAY-ROOT-MISSING).
    assert overlay_env({'PATH': 'fixture'}) == {
        'PATH': 'fixture', OVERLAY_ROOT_MISSING: str(tmp_path / 'opencode.jsonc')}


def test_probe_admission(monkeypatch):
    # Pin a present (empty) root config so the reason does not depend on the host's AUTOOS_ROOT.
    from plangraph.routing_support import opencode_overlay
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    row = {'id': 'omniroute/cx', 'harness': 'opencode'}
    assert resolver.host_skip_reason(row, probe=lambda env: {'other/model'}) == 'not_served_on_host: omniroute/cx'
    assert resolver.host_skip_reason(row, probe=lambda env: None) is None
    assert resolver.host_skip_reason(row, probe=lambda env: {'omniroute/cx'}) is None


@pytest.mark.parametrize('failure', ['timeout', 'missing', 'nonzero'])
def test_probe_timeout(monkeypatch, failure):
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('opencode', 10)
        if failure == 'missing':
            raise FileNotFoundError('opencode')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(subprocess, 'run', timeout)
    resolver._served_models_real.cache_clear()
    assert resolver._served_models_real('{}') is None
    assert len(calls) == 1
    assert resolver.host_skip_reason({'id': 'missing/model', 'harness': 'opencode'},
                                    probe=lambda env: resolver._served_models_real('{}')) is None


@pytest.mark.parametrize('all_bad,no_fallthrough,count,status', [
    (False, False, 2, 'completed'), (True, False, 2, 'failed'),
    (False, True, 1, 'failed')])
def test_runtime_unavailable(tmp_path, monkeypatch, all_bad, no_fallthrough, count, status):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import JobInfo, spawn_service
    legs = DEFAULT_LEGS[:2]
    calls = []
    async def spawn(**kw):
        calls.append(kw)
        bad = all_bad or len(calls) == 1
        return JobInfo('job-' + str(len(calls)), 'failed' if bad else 'completed',
                       'x', kw['model_or_combo'], exit_code=1 if bad else 0,
                       stderr='Error: Model unavailable: ' + kw['model_or_combo'] if bad else '')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(state=tmp_path, runner=runner)
    plan = SpawnPlan(tuple(legs), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda row: None)
    async def continuation(*a):
        pass
    monkeypatch.setattr(service, '_continue_writer', continuation)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x',
                         wait_seconds=None, no_fallthrough=no_fallthrough))
    assert len(calls) == count
    assert result['status'] == status
    assert result['attempts'][0]['skip_reason'].startswith('Error: Model unavailable:')
    if all_bad:
        assert all(a['skip_reason'] in result['error'] for a in result['attempts'])
    elif count == 2:
        assert result['attempts'][1]['skip_reason'] is None
