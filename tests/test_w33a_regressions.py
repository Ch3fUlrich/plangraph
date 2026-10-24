import asyncio
import time

import pytest

from plangraph.spawner.env_redact import redact_env_values, unavailable_reason
from plangraph.spawner.runner import SpawnerRunner
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('text', [
    'Review complete.\nunknown model was mentioned in the diff',
    'Edited parser; TODO: handle unknown model string in fixture',
    '{"message":"unknown model", "error":false}',
])
def test_stdout_prose_is_not_unavailable(text):
    assert unavailable_reason(1, text, '') is None


@pytest.mark.parametrize('text', ['ERROR: unknown model',
    '{"error":{"message":"model unavailable"}}',
    '[{"error":"not served"}]', 'HTTP 404: unknown model'])
def test_stdout_error_is_unavailable(text):
    assert unavailable_reason(1, text, '') == text
    assert unavailable_reason(0, text, '') is None


@pytest.mark.parametrize('marker', ['model unavailable', 'NOT SERVED', 'unknown model', 'not_served_on_host'])
def test_stderr_diagnostics(marker):
    assert unavailable_reason(1, '', 'prefix ' + marker)


def test_redaction_values_and_multiline():
    assert redact_env_values('beef 8192 true', {'GATEWAY_TOKEN': 'beef', 'TOKEN_LIMIT': '8192', 'AUTH': 'true'}) == 'beef 8192 true'
    assert redact_env_values('abcdefgh', {'MY_SECRET': 'abcdefgh'}) == '[autoos:redacted]'
    env = {'MY_SECRET': 'first-secret-line\nsecond-secret-line\nthird-secret-line'}
    for line in env['MY_SECRET'].splitlines():
        assert line not in redact_env_values(line, env)
    assert redact_env_values('abc', {'OPENCODE_CONFIG_CONTENT': '{"providers":{"x":{"env":["ODD"]}}}', 'ODD': 'abc'}) == '[autoos:redacted]'


def test_multiline_secret_in_individual_child_lines():
    secret = 'first-secret-line\nsecond-secret-line\nthird-secret-line'
    output = '\n'.join(redact_env_values(line, {'PRIVATE_KEY': secret})
                       for line in secret.splitlines())
    assert output == '\n'.join(['[autoos:redacted]'] * 3)


def test_identifier_stays_addressable(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_SECRET', 'abcdefgh')
    service = SpawnService(state=tmp_path)
    run_id = 'run-abcdefghend'
    service._save(dict(run_id=run_id, status='completed', stdout_tail='abcdefgh',
                       nested={'id': 'abcdefgh', 'branch': 'abcdefgh'}))
    result = service.result(run_id)
    assert result['run_id'] == run_id
    assert result['nested'] == {'id': 'abcdefgh', 'branch': 'abcdefgh'}
    assert 'abcdefgh' not in result['stdout_tail']


def test_host_probe_ticks_and_cleans_env(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    def probe(*args, **kwargs):
        time.sleep(.3)
        return set()
    monkeypatch.setattr(resolver, '_served_models_real', probe)
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: 'not_served_on_host: fixture')
    async def run():
        ticks = []
        async def tick():
            while True:
                await asyncio.sleep(.01)
                ticks.append(True)
        ticker = asyncio.create_task(tick())
        try:
            job = await runner.spawn(prompt='test', model_or_combo=DEFAULT_LEGS[0].model, client='opencode')
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        assert job.status == 'failed'
        assert len(ticks) >= 20
        assert runner._child_envs == {}
    asyncio.run(run())
