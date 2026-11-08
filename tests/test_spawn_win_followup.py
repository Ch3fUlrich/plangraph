"""Hermetic stdin-delivery and Windows parity follow-up regressions."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from plangraph.spawner.runner import SpawnerRunner, _resolve_launch
from plangraph.routing_support.clients import build_standalone_fallback_command, launch_stdin_payload
from plangraph.routing_support.opencode_render import REFUSAL_TOO_LARGE, REFUSAL_UNRESOLVED, render_gateway_providers


@pytest.mark.parametrize('exe', ['qodercli.exe', 'qodercli.cmd'])
def test_long_qoder_stdin(exe):
    prompt = 'é\n"&' * 12000
    cmd = build_standalone_fallback_command('qodercli', prompt, 'qoder/test')
    payload = launch_stdin_payload(cmd, prompt, exe, is_nt=True)
    assert payload == prompt.encode('utf-8')
    assert prompt not in cmd
    assert cmd[-2:] == ['--input-format', 'text']
    assert _resolve_launch(exe, cmd[1:], is_nt=True)[1] is None


def test_size_refusal_does_not_mutate_env():
    import json
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'padding': 'x' * 31000,
           'providers': {'mimo': {'settings': {'baseURL': 'https://gateway.invalid/v1'}}}})}
    before = dict(env)
    assert render_gateway_providers(env, 'mimo/mimo-v2.6-flash', is_nt=True) == REFUSAL_TOO_LARGE
    assert env == before


def test_runner_long_qoder_uses_existing_stdin_path(monkeypatch):
    async def run():
        prompt = 'x' * 40000
        proc = AsyncMock()
        proc.pid = None
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'VERDICT: ACCEPT', b''))
        factory = AsyncMock(return_value=proc)
        monkeypatch.setattr('plangraph.spawner.runner._is_nt', lambda: True)
        monkeypatch.setattr('plangraph.spawner.target.resolve_target', lambda model: (model, None))
        monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: None)
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
        job = await SpawnerRunner(process_factory=factory).spawn(
            prompt, 'Qwen3.8-Flash', client='qodercli', role='review', wait_seconds=2)
        assert job.status == 'completed'
        assert prompt not in factory.call_args.args
        assert factory.call_args.kwargs['stdin'] == asyncio.subprocess.PIPE
        proc.communicate.assert_awaited_once_with(input=prompt.encode())
        assert job.stdout == 'VERDICT: ACCEPT'
    asyncio.run(run())


def test_rejected_stdin_never_retries_empty(monkeypatch):
    class RejectingProcess:
        returncode = 0
        empty_calls = 0

        async def communicate(self, **kwargs):
            if 'input' in kwargs:
                raise TypeError('unsupported input')
            self.empty_calls += 1
            return b'empty prompt completed', b''

    async def run():
        proc = RejectingProcess()
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
        runner = SpawnerRunner(process_factory=AsyncMock(return_value=proc))
        job = await runner.spawn('test', 'claude/claude-haiku-4-5', client='claude', role='review', wait_seconds=2)
        assert job.status == 'failed'
        assert job.exit_code != 0
        assert 'stdin_delivery_failed' in job.stderr
        assert proc.empty_calls == 0
    asyncio.run(run())


def test_runner_without_gateway_url_refuses(monkeypatch):
    for name in ('PLANGRAPH_GATEWAY_URL', 'OMNIROUTE_BASE_URL', 'AUTOOS_OMNIROUTE_URL', 'OPENCODE_CONFIG_CONTENT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('plangraph.spawner.opencode_overlay.overlay_env', lambda env: env)

    async def run():
        factory = AsyncMock()
        job = await SpawnerRunner(process_factory=factory).spawn('test', 'mimo/mimo-v2.6-flash', role='review')
        assert job.status == 'failed'
        assert job.stderr == REFUSAL_UNRESOLVED
        factory.assert_not_called()
    asyncio.run(run())


def test_async_admission_preserves_render_refusal(monkeypatch):
    from plangraph.routing_support import resolver, opencode_render
    probe = AsyncMock()
    monkeypatch.setattr(resolver, 'host_probe_env', lambda env=None: {})
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda env, model: REFUSAL_TOO_LARGE)
    monkeypatch.setattr(resolver, 'served_models_async', probe)
    reason = asyncio.run(resolver.host_skip_reason_async(
        {'model': 'mimo/mimo-v2.6-flash', 'harness': 'opencode'}))
    assert reason == REFUSAL_TOO_LARGE
    probe.assert_not_awaited()
