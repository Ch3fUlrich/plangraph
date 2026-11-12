"""R-orch-19: large reviews must cross real exec boundaries, not mocked Popen."""
import asyncio
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import SpawnerRunner, spawn_service
from plangraph.routing_support.clients import build_command, launch_stdin_payload


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux MAX_ARG_STRLEN regression')
@pytest.mark.parametrize('size', [200 * 1024, 300 * 1024])
def test_real_review_child_reaches_leg(tmp_path, monkeypatch, size):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import leg_probe, resolver, runner as runner_module
    binary = tmp_path / 'agy'
    binary.write_text(
        f'#!{sys.executable}\n'
        'import hashlib, json, sys\n'
        'data = sys.stdin.buffer.read()\n'
        'prompt = json.loads(data)["message"]["content"]\n'
        'print(json.dumps({"event":"result", "result":{"status":"SUCCESS",'
        '"response":"VERDICT: ACCEPT " + hashlib.sha256(prompt.encode()).hexdigest()}}))\n'
    )
    binary.chmod(0o700)
    # No inherited PATH, gateway endpoint, state, credentials, or notification hook.
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    monkeypatch.setattr(runner_module, 'build_child_env', lambda *a, **kw: dict(PATH=str(tmp_path)))
    async def no_probe(*a, **kw):
        return None
    monkeypatch.setattr(leg_probe, 'before_launch', no_probe)
    async def fixed_plan(plan, **kw):
        return plan
    monkeypatch.setattr(resolver, 'plan_for_launch_async', fixed_plan)
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'agy-gemini-3.8-flash')
    prompt = 'review this change\n' + 'x' * (size - 20) + '\n\n'

    async def run():
        # Default process factory executes the real Python OOM child entry point.
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        sink = SimpleNamespace(deliver=lambda *a, **kw: None)
        service = spawn_service(runner, state=tmp_path / 'state', completion_sink=sink, keeper_sink=sink)
        monkeypatch.setattr(service, 'admit_spawn', lambda *a, **kw: None)
        monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((leg,), (), 'test', (), ()))
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt=prompt,
            role='review', writer_families=['gpt'], wait_seconds=None)
        assert record['status'] == 'completed', record.get('error')
        assert hashlib.sha256(prompt.encode()).hexdigest() in record['stdout_tail']
        assert record['attempts'][0]['status'] == 'completed'
        assert runner.jobs[record['job_id']].command[0] == 'agy'
        assert runner.jobs[record['job_id']].exit_code == 0
        assert service.result(record['run_id'])['status'] == 'completed'
    asyncio.run(run())


@pytest.mark.parametrize('harness', ['opencode', 'agy', 'qodercli', 'codex', 'claude', 'gemini', 'qwen'])
@pytest.mark.parametrize('prompt', ['short\n\né', 'x' * (300 * 1024)], ids=['short', '300KiB'])
def test_every_harness_uses_stdin(harness, prompt):
    cmd = build_command(prompt, 'fixture/model', client=harness)
    assert prompt not in cmd
    payload = launch_stdin_payload(cmd, prompt, harness)
    if harness == 'agy':
        assert json.loads(payload)['message']['content'] == prompt
    else:
        assert payload == prompt.encode()


@pytest.mark.parametrize('exit_code', [None, 0, 1])
@pytest.mark.parametrize('previous', [None, 'harness_limit'])
def test_pre_exec_prompt_refusal_has_named_reason(exit_code, previous):
    from plangraph.spawner.exit_outcome import terminal_reason
    record = dict(status='failed', exit_code=exit_code, reason=previous,
                  error='inline_diff_failed: prompt_too_large: request limit')
    terminal_reason(record)
    assert record['reason'] == 'prompt_too_large'


@pytest.mark.parametrize('harness', ['agy', 'qodercli'])
@pytest.mark.parametrize('tail', [[], ['-p'], ['-p', 'private task']])
def test_runner_transport_refusal_is_named(tmp_path, monkeypatch, harness, tail):
    from plangraph.spawner import runner as module
    from plangraph.spawner.exit_outcome import terminal_reason
    from unittest.mock import AsyncMock
    monkeypatch.setattr(module, 'build_command', lambda **kw: [harness, *tail])
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
    factory = AsyncMock()
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: model)
    model = 'gemini-3.8-flash' if harness == 'agy' else 'fixture-qoder-model'
    job = asyncio.run(runner.spawn('private task', model, role='review', client=harness))
    assert job.status == 'failed'
    record = dict(status=job.status, exit_code=job.exit_code, stderr_tail=job.stderr)
    terminal_reason(record, job)
    assert record['reason'] == 'stdin_prompt_flag_missing'
    factory.assert_not_called()


@pytest.mark.parametrize('routed', [False, True])
def test_over_leg_limit_has_named_record_reason(tmp_path, monkeypatch, routed):
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import service as base
    from plangraph.spawner import leg_probe
    leg = replace(next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-3.8-flash'), max_request_tokens=100)
    async def no_probe(*a):
        return None
    monkeypatch.setattr(leg_probe, 'before_launch', no_probe)

    async def run():
        service = spawn_service(state=tmp_path) if routed else base.SpawnService(state=tmp_path)
        service.runner.default_cwd = str(tmp_path)
        monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((leg,), (), 'test', (), ()))
        record = await service.spawn(card={'estimate_tokens': 100}, prompt='x' * 301,
            role='review', writer_families=['gpt'], wait_seconds=None)
        assert record['status'] == 'failed'
        assert record['reason'] == 'prompt_too_large'
        assert 'Errno 7' not in json.dumps(record)
        assert service.result(record['run_id'])['reason'] == 'prompt_too_large'
    asyncio.run(run())
