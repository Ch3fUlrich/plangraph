"""Late-seat regressions for probe admission and terminal run evidence."""
import asyncio
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from plangraph.planner.legs import Leg
from plangraph.routing_errors import ParameterRefused
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner import cli, exit_outcome, leg_probe, proc_kill
from plangraph.spawner.run_watchdog import terminal_record


@pytest.mark.parametrize('entry', ['probe', 'before_launch'])
@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'invalid-probe-duration'])
def test_invalid_probe_timeout_is_refused_without_liveness_evidence(
        tmp_path, monkeypatch, entry, value):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = NS(leg_reader=store, spawn=AsyncMock(side_effect=RuntimeError('host spawn failed')))
    leg = NS(model='provider/model', harness='agy')
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', value)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    for _ in range(2):
        with pytest.raises(ValueError, match='invalid PLANGRAPH_LEG_PROBE_TIMEOUT_S'):
            asyncio.run(leg_probe.probe(runner, leg, store=store) if entry == 'probe' else
                        leg_probe.before_launch(runner, leg, 'implement', False))
    runner.spawn.assert_not_called()
    assert store.leg_status(leg.model)['state'] == 'ok'
    assert not store.path.exists()
    assert leg_probe.seconds('PLANGRAPH_LEG_PROBE_TTL_S', 1800, 0) == 0


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf', 'invalid-probe-duration'])
def test_invalid_probe_ttl_is_refused_before_store_resolution(monkeypatch, value):
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', value)
    def store(runner):
        pytest.fail('invalid TTL must refuse before resolving the store')
    monkeypatch.setattr(leg_probe, '_store_for', store)
    with pytest.raises(ValueError, match='invalid PLANGRAPH_LEG_PROBE_TTL_S'):
        asyncio.run(leg_probe.before_launch(None, NS(model='provider/model'), 'implement', False))


@pytest.mark.parametrize('error', [ValueError, RuntimeError, OSError])
def test_probe_spawn_error_is_infrastructure_not_leg_liveness(tmp_path, error):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = NS(spawn=AsyncMock(side_effect=error('host spawn failed')))
    leg = NS(model='provider/model', harness='agy')
    for _ in range(2):
        result = asyncio.run(leg_probe.probe(runner, leg, timeout_s=5, store=store))
        assert result['ok'] is False
        assert result['infra'] is True
        assert result['reason'].startswith('probe infra: spawn')
    assert store.leg_status(leg.model)['state'] == 'ok'
    assert not store.path.exists()


@pytest.mark.parametrize('case', ['missing-leg', 'bad-leg', 'bad-env', 'zero-timeout', 'bad-timeout'])
@pytest.mark.parametrize('json_output', [False, True])
def test_probe_cli_returns_structured_parameter_refusal(
        tmp_path, monkeypatch, capsys, case, json_output):
    from plangraph.planner import legs
    leg = Leg('sample', 'free', 'agy', 'sample-model', 'synthetic', (), 1000, False,
              roles=('implement',))
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    runner = NS(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'),
                spawn=AsyncMock(side_effect=RuntimeError('host spawn failed')))
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: NS(runner=runner))
    args = ['probe'] + (['--json'] if json_output else [])
    if case == 'bad-leg':
        args += ['--leg', 'unknown-leg']
    elif case in ('bad-env', 'zero-timeout'):
        args += ['--leg', leg.id]
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S',
                           'invalid-probe-duration' if case == 'bad-env' else '0')
    elif case == 'bad-timeout':
        args += ['--leg', leg.id, '--timeout', '-1']
    assert cli.main(args) == 2
    output = capsys.readouterr()
    refusal = json.loads(output.out)
    assert refusal['status'] == 'refused'
    assert refusal['refusal'] == 'ParameterRefused'
    assert refusal['exit_code'] == 2
    expected_error = {'missing-leg': 'select --leg ID or --all-writers',
                      'bad-leg': 'unknown legs: unknown-leg',
                      'bad-timeout': 'invalid --timeout'}.get(
                          case, 'invalid PLANGRAPH_LEG_PROBE_TIMEOUT_S')
    assert refusal['error'] == expected_error
    assert 'Traceback' not in output.err
    assert 'invalid-probe-duration' not in output.out + output.err
    runner.spawn.assert_not_called()
    assert not runner.leg_reader.path.exists()


@pytest.mark.parametrize('json_output', [False, True])
def test_probe_cli_internal_value_error_is_not_parameter_refusal(
        tmp_path, monkeypatch, capsys, json_output):
    from plangraph.planner import legs
    leg = Leg('sample', 'free', 'agy', 'sample-model', 'synthetic', (), 1000, False,
              roles=('implement',))
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    runner = NS(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: NS(runner=runner))
    probe = AsyncMock(side_effect=ValueError('internal probe failure'))
    monkeypatch.setattr(leg_probe, 'probe', probe)
    args = ['probe', '--leg', leg.id, '--timeout', '5'] + (['--json'] if json_output else [])
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'internal probe failure\n'
    probe.assert_awaited_once()
    assert not runner.leg_reader.path.exists()


@pytest.mark.parametrize('entry,label', [('probe', 'timeout_s'), ('probe_many', '--timeout')])
@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), 'invalid-probe-duration'])
def test_explicit_probe_timeout_refusal_names_parameter(
        tmp_path, monkeypatch, entry, label, value):
    from plangraph.planner import legs
    leg = Leg('sample', 'free', 'agy', 'sample-model', 'synthetic', (), 1000, False,
              roles=('implement',))
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = NS(leg_reader=store, spawn=AsyncMock())
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', '180')
    with pytest.raises(ParameterRefused, match='^invalid ' + label + '$'):
        asyncio.run(leg_probe.probe(runner, leg, timeout_s=value, store=store) if entry == 'probe'
                    else leg_probe.probe_many(runner, [leg.id], timeout=value))
    runner.spawn.assert_not_called()
    assert not store.path.exists()


def test_parameter_refused_is_a_value_error():
    assert issubclass(ParameterRefused, ValueError)


def test_probe_mcp_internal_value_error_is_not_parameter_refusal(tmp_path, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    probe_many = AsyncMock(side_effect=ValueError('internal probe failure'))
    monkeypatch.setattr(leg_probe, 'probe_many', probe_many)
    with pytest.raises(ValueError, match='^internal probe failure$'):
        asyncio.run(server.call_tool('plangraph_agent_probe', {'leg': ['sample']}))
    probe_many.assert_awaited_once()


@pytest.mark.parametrize('status', ['completed', 'dry_run', 'failed', 'skipped'])
def test_provider_stop_reason_requires_failed_or_skipped_job(tmp_path, monkeypatch, status):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.service import SpawnService
    leg = Leg('sample', 'subscription', 'opencode', 'provider/model', 'synthetic', (), 1, False)
    job = NS(job_id='sample', status=status, exit_code=1 if status == 'failed' else 0,
             stdout='done', stderr='', served_model=None, served_family=None,
             served_witness_reason=None, events=[])
    runner = NS(_processes={}, _tasks={}, jobs={'sample': job},
                provider_responses={'sample': {'status': 429}}, spawn=AsyncMock(return_value=job))
    service = SpawnService(runner, state=tmp_path)
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'task')
    record = asyncio.run(service._spawn(card={'title': 'synthetic'}, role='research',
        wait_seconds=None, _plan=SpawnPlan((leg,), (), 'test', (), ())))
    assert record['status'] == status
    assert record['attempts'][0]['skip_reason'] == (
        'provider_stop' if status in ('failed', 'skipped') else None)


@pytest.mark.parametrize('status', ['stalled', 'timeout'])
@pytest.mark.parametrize('role', ['implement', 'review'])
def test_terminal_record_without_watchdog_event_keeps_terminal_outcome(status, role):
    earlier = {'event': 'annotated'}
    event = {'event': 'run_timeout'}
    record = {'role': role, 'events': [earlier]}
    job = NS(status=status, exit_code=124, events=[event])
    assert terminal_record(record, job) is True
    assert record['status'] == ('timed_out' if status == 'timeout' and role == 'review' else status)
    assert record['reason'] == status and record['exit_code'] == 124
    assert record['watchdog'] is None
    assert record['events'] == [earlier, event]


@pytest.mark.skipif(os.name == 'nt', reason='supervisor orphan detection is POSIX-only')
@pytest.mark.parametrize('observed_start,signal_error', [
    (None, None), (None, PermissionError), (None, ProcessLookupError),
    (100, None), (100, PermissionError), (100, ProcessLookupError), (101, None)])
def test_orphaned_checks_supervisor_pid_start_time(
        tmp_path, monkeypatch, observed_start, signal_error):
    path = tmp_path / 'supervisor.stderr.log'
    path.with_suffix(path.suffix + '.pid').write_text('12345')
    record = dict(status='running', ended=None, supervisor_stderr_path=str(path),
                  supervisor_pid_start=100)
    stat = Mock(return_value=('S', 1, 12345, 12345, observed_start)
                if observed_start is not None else None)
    monkeypatch.setattr(proc_kill, '_stat', stat)
    signal = Mock(side_effect=signal_error)
    monkeypatch.setattr(os, 'kill', signal)
    result = exit_outcome.orphaned(record, 200)
    stat.assert_called_once_with(12345)
    if observed_start == 101:
        signal.assert_not_called()
    else:
        signal.assert_called_once_with(12345, 0)
    if observed_start in (None, 100) and signal_error is not ProcessLookupError:
        assert result is None
    else:
        assert result['status'] == 'failed' and result['reason'] == 'orphaned'
        assert result['ended'] == 200 and result['exit_code'] == 1
    assert record['status'] == 'running' and record['ended'] is None


@pytest.mark.skipif(os.name != 'nt', reason='requires a real Windows runner')
def test_orphaned_on_windows_never_imports_proc_stat_or_signals(tmp_path, monkeypatch):
    import builtins
    path = tmp_path / 'supervisor.stderr.log'
    path.with_suffix(path.suffix + '.pid').write_text('12345')
    record = dict(status='running', ended=None, supervisor_stderr_path=str(path),
                  supervisor_pid_start=100)
    original_import = builtins.__import__
    def checked_import(name, *args, **kwargs):
        assert not name.endswith('proc_kill')
        return original_import(name, *args, **kwargs)
    signal = Mock(side_effect=AssertionError('Windows must never probe with os.kill'))
    monkeypatch.setattr(os, 'kill', signal)
    monkeypatch.setattr(builtins, '__import__', checked_import)
    result = exit_outcome.orphaned(record, 200)
    assert result is None
    signal.assert_not_called()


def test_orphaned_windows_guard_precedes_process_stat_and_signal():
    import ast
    import inspect
    function = ast.parse(inspect.getsource(exit_outcome.orphaned)).body[0]
    guard = next(node for node in function.body if isinstance(node, ast.If))
    assert any(isinstance(node, ast.Compare) and ast.unparse(node) == "os.name == 'nt'"
               for node in ast.walk(guard.test))
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Return)
    assert isinstance(guard.body[0].value, ast.Constant) and guard.body[0].value.value is None
    process_import = next(node for node in ast.walk(function)
                          if isinstance(node, ast.ImportFrom) and node.module == 'proc_kill')
    signal = next(node for node in ast.walk(function)
                  if isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.kill')
    assert guard.end_lineno < process_import.lineno < signal.lineno
