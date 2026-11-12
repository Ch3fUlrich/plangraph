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


@pytest.mark.parametrize('harness', ['opencode', 'agy', 'qoder', 'qodercli', 'codex', 'claude', 'gemini', 'qwen'])
@pytest.mark.parametrize('prompt', ['short\n\né', 'x' * (300 * 1024)], ids=['short', '300KiB'])
def test_every_harness_uses_stdin(harness, prompt):
    cmd = build_command(prompt, 'fixture/model', client=harness)
    assert prompt not in cmd
    payload = launch_stdin_payload(cmd, prompt, harness)
    if harness == 'agy':
        assert json.loads(payload)['message']['content'] == prompt
    else:
        assert payload == prompt.encode()


@pytest.mark.parametrize('command', [['gateway-client', 'run'], ['legacy-client', '-p', ''],
                                    ['legacy-client', '-p', 'task'], ['claude', '--bg', 'task'],
                                    ['opencode', 'serve'], ['codex', 'exec', 'task']])
def test_legacy_commands_do_not_drop_stdin(command):
    from plangraph.routing_support.clients import PromptTransportError
    prompt = 'review payload\né\n' + 'x' * (300 * 1024)
    # Refuse explicitly rather than silently dropping or duplicating a task.
    original = command.copy()
    with pytest.raises(PromptTransportError, match='stdin_prompt_command_unknown:'):
        launch_stdin_payload(command, prompt, command[0])
    assert command == original


@pytest.mark.parametrize('harness', ['opencode', 'agy', 'qoder', 'qodercli', 'codex', 'claude', 'gemini', 'qwen'])
@pytest.mark.parametrize('embedded', [False, True])
def test_stdin_refuses_a_second_argv_carrier(harness, embedded):
    from plangraph.routing_support.clients import PromptTransportError
    prompt = 'private task'
    cmd = build_command(prompt, 'fixture/model', client=harness)
    cmd.append('--prompt=' + prompt if embedded else prompt)
    with pytest.raises(PromptTransportError, match='stdin_prompt_argv_conflict:'):
        launch_stdin_payload(cmd, prompt, harness)


def test_prompt_only_launcher_is_a_known_stdin_reader(monkeypatch):
    from plangraph.routing_support.review_mode import is_prompt_only_command
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{}')
    prompt = 'review payload\né\n'
    cmd = build_command(prompt, 'fixture/model', client='opencode', role='review', tool_mode='prompt-only')
    assert is_prompt_only_command(cmd)
    assert prompt not in cmd
    assert launch_stdin_payload(cmd, prompt, cmd[0]) == prompt.encode()


@pytest.mark.parametrize('field', ['error', 'stderr', 'stderr_tail'])
@pytest.mark.parametrize('named', ['prompt_too_large', 'stdin_prompt_flag_missing'])
def test_persisted_diagnostic_keys_keep_named_reason(field, named):
    from plangraph.spawner.exit_outcome import terminal_reason
    record = dict(status='failed', exit_code=1, **{field: named + ': refused before exec'})
    if field != 'error':
        record['events'] = [dict(event='prompt_transport_refused', reason=named)]
    terminal_reason(record)
    assert record['reason'] == named


@pytest.mark.parametrize('exit_code', [None, 0, 1])
@pytest.mark.parametrize('previous', [None, 'harness_limit'])
def test_pre_exec_prompt_refusal_has_named_reason(exit_code, previous):
    from plangraph.spawner.exit_outcome import terminal_reason
    record = dict(status='failed', exit_code=exit_code, reason=previous,
                  error='inline_diff_failed: prompt_too_large: request limit')
    terminal_reason(record)
    assert record['reason'] == 'prompt_too_large'


@pytest.mark.parametrize('field', ['stderr', 'stderr_tail', 'job_stderr'])
@pytest.mark.parametrize('text', ['harness echoed prompt_too_large: task text',
                               'prompt_too_large: task text'])
def test_harness_echo_is_not_prompt_refusal(field, text):
    from plangraph.spawner.exit_outcome import terminal_reason
    record = dict(status='failed', exit_code=1)
    if field != 'job_stderr':
        record[field] = text
    terminal_reason(record, SimpleNamespace(stderr=text, command=['agy']) if field == 'job_stderr' else None)
    assert record.get('reason') != 'prompt_too_large'


def test_prompt_refusal_must_start_spawner_error():
    from plangraph.spawner.exit_outcome import terminal_reason
    record = dict(status='failed', exit_code=1, error='harness echoed prompt_too_large: task text')
    terminal_reason(record)
    assert record['reason'] != 'prompt_too_large'


@pytest.mark.parametrize('role', ['implement', 'edit', 'review'])
@pytest.mark.parametrize('tool_mode', ['tools', 'inline-only'])
@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('strict', ['0', '1'])
def test_leg_without_request_limit_does_not_crash(tmp_path, monkeypatch, role, tool_mode, missing, strict):
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    from plangraph.spawner.context_pack import Pack
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', strict)
    leg = SimpleNamespace(harness='agy', model='fixture/model', tool_mode=tool_mode)
    if not missing:
        leg.max_request_tokens = None
    seat = SeatSpawnMixin()
    seat.state, seat.runner = tmp_path, SimpleNamespace(default_cwd=str(tmp_path))
    assert seat._leg_prompt('task', {}, role, leg, Pack(), document_review=True) == 'task'


def test_missing_budget_oversized_diff_is_named(tmp_path, monkeypatch):
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    from plangraph.spawner.context_pack import Pack, PromptBoundError
    from plangraph.spawner.inline_diff import InlineDiffError
    def oversized(*args, **kwargs):
        assert kwargs['max_chars'] == 60000
        raise InlineDiffError('oversized', chars=60001)
    monkeypatch.setattr('plangraph.spawner.inline_diff.inline_diff_prompt', oversized)
    seat = SeatSpawnMixin()
    seat.state, seat.runner = tmp_path, SimpleNamespace(default_cwd=str(tmp_path))
    leg = SimpleNamespace(harness='opencode', model='fixture/model', tool_mode='inline-only')
    with pytest.raises(PromptBoundError, match='^prompt_too_large:'):
        seat._leg_prompt('review', {}, 'review', leg, Pack())


@pytest.mark.parametrize('role', ['implement', 'edit'])
@pytest.mark.parametrize('tool_mode', ['tools', 'inline-only', 'prompt-only'])
def test_non_review_prompt_above_bound_remains_accepted(tmp_path, monkeypatch, role, tool_mode):
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    from plangraph.spawner.context_pack import Pack
    monkeypatch.delenv('PLANGRAPH_CONTEXT_STRICT', raising=False)
    leg = SimpleNamespace(harness='agy', model='fixture/model', tool_mode=tool_mode, max_request_tokens=100)
    seat = SeatSpawnMixin()
    seat.state, seat.runner = tmp_path, SimpleNamespace(default_cwd=str(tmp_path))
    prompt = 'x' * 301
    assert seat._leg_prompt(prompt, {}, role, leg, Pack()) == prompt


@pytest.mark.parametrize('harness', ['agy', 'qodercli'])
@pytest.mark.parametrize('tail', [[], ['-p'], ['-p', 'private task']])
@pytest.mark.parametrize('routed', [False, True])
def test_runner_transport_refusal_is_named(tmp_path, monkeypatch, harness, tail, routed):
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import runner as module
    from plangraph.spawner import leg_probe, resolver, service as base
    from unittest.mock import AsyncMock
    monkeypatch.setattr(module, 'build_command', lambda **kw: [harness, *tail])
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', AsyncMock(return_value=None))
    factory = AsyncMock()
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: model)
    from plangraph.planner import legs
    leg = replace(next(l for l in DEFAULT_LEGS if l.harness == harness and 'inline' not in l.tool_mode), disabled=None)
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    monkeypatch.setattr(leg_probe, 'before_launch', AsyncMock(return_value=None))
    async def fixed_plan(plan, **kw):
        return plan
    monkeypatch.setattr(resolver, 'plan_for_launch_async', fixed_plan)

    async def run():
        sink = SimpleNamespace(deliver=lambda *a, **kw: None)
        service = (spawn_service(runner, state=tmp_path, completion_sink=sink, keeper_sink=sink)
                   if routed else base.SpawnService(runner, state=tmp_path))
        service.completion_sink = service.keeper_sink = sink
        monkeypatch.setattr(service, 'admit_spawn', lambda *a, **kw: None)
        monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((leg,), (), 'test', (), ()))
        record = await service.spawn(card={'estimate_tokens': 100}, prompt='private task',
            role='review', writer_families=['gpt'], wait_seconds=None)
        assert 'job_id' in record, record
        job = runner.jobs[record['job_id']]
        assert job.status == record['status'] == 'failed'
        assert 'stdin_prompt_flag_missing:' in job.stderr
        assert record['reason'] == 'stdin_prompt_flag_missing'
        assert record['attempts'][0]['stderr_tail'] == job.stderr
        assert service.result(record['run_id'])['reason'] == 'stdin_prompt_flag_missing'
    asyncio.run(run())
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
