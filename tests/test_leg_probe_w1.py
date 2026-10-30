"""W1: leg state visibility, probe robustness, and quota-line detection.

Pins the spawner-stall-watchdog fixes: down/limited rows visible to
account/combo readers, two consecutive probe failures before down, failed
probes never fresh evidence, git/gpg/hooks probe infra isolated from leg
failures, timeout mid-spawn cancels the child, single-flight probes per leg,
probe writes through the store the planner reads, and error-shaped/anchored
stderr quota detection.
"""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state
from plangraph.routing_support.leg_state import LegStateStore


def _leg():
    return SimpleNamespace(model='agy/model', harness='agy')


def test_probe_rows_visible_to_account_combo_readers(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=100, down_s=60)
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=101, down_s=60)
    state = store.leg_status('agy/model', now=102, account='acct', combo='cmb')
    assert state['state'] == 'down'
    assert state['evidence']['body_excerpt'] == 'timeout'
    store.record_harness_limit('agy/model', 'error: usage limit reached', 'spawner:harness', now=200)
    assert store.leg_status('agy/model', now=201, account='acct', combo='cmb')['state'] == 'limited'
    # Explicitly scoped evidence stays scoped (omitted dimensions are no wildcards).
    store.record_probe_fail('agy/other', 'timeout', 'leg-probe', now=300, down_s=60,
                            account='acct', combo='cmb')
    assert store.leg_status('agy/other', now=301, account='acct', combo='cmb')['state'] == 'reprobe'
    assert store.leg_status('agy/other', now=301, account='acct', combo='other')['state'] == 'ok'


def test_two_consecutive_probe_failures_before_down(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_fail('agy/m', 'timeout', 'leg-probe', now=100, down_s=60)
    assert store.leg_status('agy/m', now=101)['state'] == 'reprobe'
    store.record_probe_fail('agy/m', 'timeout', 'leg-probe', now=102, down_s=60)
    assert store.leg_status('agy/m', now=103)['state'] == 'down'
    assert store.leg_status('agy/m', now=200)['state'] == 'reprobe'
    store.record_probe_pass('agy/m', 'leg-probe', now=201)
    store.record_probe_fail('agy/m', 'timeout', 'leg-probe', now=202, down_s=60)
    assert store.leg_status('agy/m', now=203)['state'] == 'reprobe'


def test_failed_probe_is_not_fresh_evidence(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_fail('agy/m', 'timeout', 'leg-probe', now=100, down_s=60)
    assert store.last_probe_ts('agy/m') is None
    store.record_probe_pass('agy/m', 'leg-probe', now=121)
    assert store.last_probe_ts('agy/m') == 121


def test_leg_down_s_env_is_validated(tmp_path, monkeypatch):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    for bad in ('abc', 'nan', '-5', 'inf'):
        monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', bad)
        store.record_probe_fail('agy/' + bad, 'timeout', 'leg-probe', now=100)
        state = store.leg_status('agy/' + bad, now=101)
        assert state['evidence']['reset_at'] == 1900


def test_probe_writes_through_the_reader_store(tmp_path):
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner

    async def factory(*args, **kwargs):
        return await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)

    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = SpawnerRunner(process_factory=factory)
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=.3, store=store))
    assert result['ok'] is False
    assert store.leg_status('agy/model')['state'] == 'reprobe'
    assert leg_state.leg_status('agy/model')['state'] == 'ok'


def test_harness_limit_stderr_is_error_shaped_or_anchored():
    from plangraph.spawner.env_redact import harness_limit_line
    assert harness_limit_line(1, '', 'Error: usage limit reached') == 'Error: usage limit reached'
    assert harness_limit_line(1, '', 'usage limit reached') == 'usage limit reached'
    assert harness_limit_line(1, '', 'HTTP 429: too many requests') == 'HTTP 429: too many requests'
    # Mid-line mentions in tracebacks / logs / prose are not quota evidence.
    assert harness_limit_line(1, '', 'the rate limit parser raised') is None
    assert harness_limit_line(1, '', 'tests are green; quota reached marker in docs') is None
    assert harness_limit_line(1, '',
        'Traceback (most recent call last):\n  File "x.py", line 3, in retry\n'
        '    raise after rate limits reset\nDone') is None
    assert harness_limit_line(1, '', 'a run of tests mentioning resets in the docs') is None


def test_probe_isolates_git_config_and_hooks(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner

    hostile = tmp_path / 'hostile.gitconfig'
    hooks = tmp_path / 'hostile-hooks'
    hooks.mkdir()
    failing = hooks / 'pre-commit'
    failing.write_text('#!/bin/sh\nexit 1\n')
    failing.chmod(0o755)
    hostile.write_text('[commit]\n\tgpgsign = true\n[core]\n\thooksPath = ' + str(hooks) + '\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(hostile))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')

    async def factory(*args, **kwargs):
        code = ("from pathlib import Path; import subprocess; Path('ok.txt').write_text('ok'); "
                "subprocess.run(['git', 'add', 'ok.txt'], check=True); "
                "subprocess.run(['git', 'commit', '-m', 'probe'], check=True)")
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)

    runner = SpawnerRunner(process_factory=factory)
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=20))
    assert result['ok'] is True, result


def test_probe_infra_failure_is_not_a_leg_failure(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe

    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(subprocess, 'check_output', broken)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(None, _leg(), timeout_s=5, store=store))
    assert result['ok'] is False
    assert result['infra'] is True
    assert store.leg_status('agy/model')['state'] == 'ok'


def test_before_launch_infra_does_not_skip_dispatch(monkeypatch):
    from plangraph.spawner import leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')

    async def fake(runner, leg, **kwargs):
        return dict(ok=False, infra=True, reason='probe infra: git', seconds=0)

    monkeypatch.setattr(leg_probe, 'probe', fake)
    assert asyncio.run(leg_probe.before_launch(None, _leg(), 'implement', False)) is None


def test_probe_waits_for_unregistered_session(tmp_path):
    from plangraph.spawner import leg_probe

    class Runner:
        _tasks = {}
        jobs = {}
        cancelled = []

        async def spawn(self, **kwargs):
            code = ("from pathlib import Path; import subprocess, time; time.sleep(0.3); "
                    "Path('ok.txt').write_text('ok'); "
                    "subprocess.run(['git', 'add', 'ok.txt'], check=True); "
                    "subprocess.run(['git', '-c', 'commit.gpgsign=false', 'commit', '-m', 'probe'], check=True)")
            proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code, cwd=kwargs['cwd'])
            job = SimpleNamespace(job_id='unregistered', status='running', exit_code=None)
            self.jobs[job.job_id] = job

            async def finish():
                await proc.wait()
                job.status, job.exit_code = 'completed', 0
            asyncio.ensure_future(finish())
            return job

        async def cancel(self, job_id):
            self.cancelled.append(job_id)

    runner = Runner()
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=10))
    assert result['ok'] is True, result


def test_timeout_mid_spawn_still_cancels_child():
    from plangraph.spawner import leg_probe

    class Runner:
        def __init__(self):
            self._tasks, self.jobs, self.cancelled = {}, {}, []

        async def spawn(self, **kwargs):
            await asyncio.sleep(0.5)  # the budget expires while spawn is in flight
            job = SimpleNamespace(job_id='slow-spawn', status='running', exit_code=None)
            self.jobs[job.job_id] = job
            self._tasks[job.job_id] = asyncio.ensure_future(asyncio.sleep(3600))
            return job

        async def cancel(self, job_id):
            self.cancelled.append(job_id)
            self.jobs[job_id].status = 'cancelled'
            task = self._tasks.get(job_id)
            if task:
                task.cancel()

    runner = Runner()
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=0.2))
    assert result['ok'] is False
    assert result['reason'] == 'timeout'
    assert runner.cancelled == ['slow-spawn']


def test_probe_is_single_flight_per_leg(monkeypatch):
    from plangraph.spawner import leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    calls = []

    async def fake(runner, leg, **kwargs):
        calls.append(1)
        await asyncio.sleep(0.15)
        leg_probe.leg_state.record_probe_pass('agy/model', 'leg-probe')
        return dict(ok=True, reason='', seconds=0)

    monkeypatch.setattr(leg_probe, 'probe', fake)

    async def run():
        return await asyncio.gather(
            *(leg_probe.before_launch(None, _leg(), 'implement', False) for _ in range(3)))

    assert asyncio.run(run()) == [None, None, None]
    assert len(calls) == 1


def test_single_flight_reuses_fresh_failure(monkeypatch):
    from plangraph.spawner import leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    calls = []

    async def fake(runner, leg, **kwargs):
        calls.append(1)
        await asyncio.sleep(0.15)
        leg_probe.leg_state.record_probe_fail('agy/model', 'timeout', 'leg-probe')
        return dict(ok=False, reason='timeout', seconds=0)

    monkeypatch.setattr(leg_probe, 'probe', fake)

    async def run():
        return await asyncio.gather(
            *(leg_probe.before_launch(None, _leg(), 'implement', False) for _ in range(3)))

    assert asyncio.run(run()) == ['down: timeout'] * 3
    assert len(calls) == 1


def test_probe_many_selection_errors():
    from plangraph.spawner.leg_probe import probe_many
    with pytest.raises(ValueError) as nothing:
        asyncio.run(probe_many(None, (), False))
    assert str(nothing.value) == 'select --leg ID or --all-writers'
    with pytest.raises(ValueError) as unknown:
        asyncio.run(probe_many(None, 'zz/nope', False))
    assert str(unknown.value) == 'unknown legs: zz/nope'


def test_mcp_probe_string_leg_is_one_leg(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer

    async def run():
        server = MCPSpawnerServer(state=tmp_path)
        result = await server.call_tool('plangraph_agent_probe', {'leg': 'zz/nope'})
        assert result.get('status') == 'refused'
        assert result['error'] == 'unknown legs: zz/nope'
        response = await server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'plangraph_agent_probe', 'arguments': {'leg': 'zz/nope'}}})
        assert response['error']['code'] == -32602
        assert response['error']['message'] == 'unknown legs: zz/nope'
    asyncio.run(run())
