"""W1: leg state visibility, probe robustness, and quota-line detection.

Pins the spawner-stall-watchdog fixes: down/limited rows visible to
account/combo readers, two consecutive probe failures before down, failed
probes never fresh evidence, git/gpg/hooks probe infra isolated from leg
failures, timeout mid-spawn cancels the child, single-flight probes per leg,
probe writes through the store the planner reads, and error-shaped/anchored
stderr quota detection.
"""
import asyncio
import sqlite3
import subprocess
import sys
import time
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
    result = asyncio.run(leg_probe.probe(runner, SimpleNamespace(model='gemini-3.8-flash', harness='agy'), timeout_s=.3, store=store))
    assert result['ok'] is False
    assert result['reason'] == 'timeout' and not result['infra']
    assert store.leg_status('agy/gemini-3.8-flash')['state'] == 'reprobe'
    assert leg_state.leg_status('agy/gemini-3.8-flash')['state'] == 'ok'


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


def test_harness_limit_omniroute_reset_after_line():
    """D-3685: OmniRoute's quota line ends in '(reset after 3h 12m 5s)' and must
    mark the leg; prose stays out (error-shaped or start-anchored only)."""
    from plangraph.spawner.env_redact import harness_limit_line
    line = 'Error: [codex/zz-model] quota threshold (reset after 3h 12m 5s)'
    assert harness_limit_line(1, '', '\x1b[1m' + line + '\x1b[0m') == line
    assert harness_limit_line(0, '', line) is None
    assert harness_limit_line(1, '', 'backoff (reset after 5 s of uptime)') is None
    # The "(reset after N)" suffix alone is no quota evidence: prose that merely
    # starts with it never marks, while an error-shaped line still does.
    assert harness_limit_line(1, '', '(reset after 5s) still running') is None
    assert harness_limit_line(1, '', 'Error: model busy (reset after 5s)') == \
        'Error: model busy (reset after 5s)'
    assert harness_limit_line(1, '', 'Error: the model exhausted their quota') == \
        'Error: the model exhausted their quota'


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
    # a model the runner's catalog accepts: the probe must reach the child commit
    leg = SimpleNamespace(model='gemini-3.8-flash', harness='agy')
    result = asyncio.run(leg_probe.probe(runner, leg, timeout_s=20))
    assert result['ok'] is True, result
    assert result['infra'] is False and result['reason'] == ''


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
    assert result['infra'] is False
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


def test_before_launch_skips_a_limited_leg(tmp_path, monkeypatch):
    """D-3685: before_launch consults leg_status before any freshness or probe
    logic: a leg booked limited is skipped -- every role, TTL 0 included, a
    newer failed probe included -- with no probe, spawn, or probe row, and is
    probed again once the booked reset has passed."""
    from plangraph.spawner import leg_probe

    now = [1000.0]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = SimpleNamespace(leg_reader=store)
    calls = []

    async def fake(probe_runner, leg, **kwargs):
        calls.append(1)
        return dict(ok=True, reason='', seconds=0, infra=False)

    monkeypatch.setattr(leg_probe, 'probe', fake)
    store.record_harness_limit('agy/model', 'error: quota threshold (reset after 3h 12m 5s)',
                               'test', now=now[0])
    # A newer failed probe never hides the future limit (the booked reset wins).
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=1010, down_s=60)

    def events():
        return sqlite3.connect(str(store.path)).execute(
            'SELECT count(*) FROM leg_events').fetchone()[0]

    for ttl in ('0', '1800'):
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', ttl)
        for role in ('implement', 'review', 'fix'):
            assert asyncio.run(leg_probe.before_launch(runner, _leg(), role, False)) == \
                'limited until 1970-01-01T03:28:45+00:00'
            assert asyncio.run(leg_probe.before_launch(runner, _leg(), role, True)) is None
    assert calls == [] and events() == 2  # 0 probes, 0 spawns, 0 writes

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    now[0] = 1000 + 11525 + 1  # past the booked reset: re-probe and launch
    assert asyncio.run(leg_probe.before_launch(runner, _leg(), 'implement', False)) is None
    assert len(calls) == 1


def test_probe_many_selection_errors():
    from plangraph.routing_errors import ParameterRefused
    from plangraph.spawner.leg_probe import probe_many
    with pytest.raises(ParameterRefused) as nothing:
        asyncio.run(probe_many(None, (), False))
    assert str(nothing.value) == 'select --leg ID or --all-writers'
    with pytest.raises(ParameterRefused) as unknown:
        asyncio.run(probe_many(None, 'zz/nope', False))
    assert str(unknown.value) == 'unknown legs: zz/nope'


def test_probe_many_skips_a_limited_leg(tmp_path, monkeypatch):
    """D-3685: probe_many skips a leg booked limited and writes no row for it;
    an unbooked leg still probes."""
    from plangraph.planner import legs as planner_legs
    from plangraph.spawner import leg_probe

    async def listed(*args):
        return None
    monkeypatch.setattr(leg_probe, 'catalog_model_check', listed)

    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(planner_legs, 'DEFAULT_LEGS', (
        planner_legs.Leg('zz-limited', 'free', 'agy', 'zz-model-a', 'other',
                         ('128k',), 1000, False, roles=('implement',)),
        planner_legs.Leg('zz-ok', 'free', 'agy', 'zz-model-b', 'other',
                         ('128k',), 1000, False, roles=('implement',))))

    class Runner:
        leg_reader = store

        async def spawn(self, **kwargs):
            return SimpleNamespace(job_id='probe', status='failed', exit_code=1)

        async def cancel(self, job_id):
            pass

    store.record_harness_limit('agy/zz-model-a',
                               'error: quota threshold (reset after 3h 12m 5s)', 'test')
    reset = store.leg_status('agy/zz-model-a')['evidence']['reset_at']

    def count(leg):
        return sqlite3.connect(str(store.path)).execute(
            'SELECT count(*) FROM leg_events WHERE leg_key=?', (leg,)).fetchone()[0]

    assert count('agy/zz-model-a') == 1
    result = asyncio.run(leg_probe.probe_many(Runner(), ['zz-limited', 'zz-ok']))
    by_id = {row['id']: row for row in result['probes']}
    # A skipped row carries the full probe-row shape (reason/seconds/infra):
    # every probe-row reader keeps working on skipped rows.
    assert by_id['zz-limited'] == dict(id='zz-limited', ok=False, reason='limited', seconds=0.0,
                                       infra=False, skipped='limited', reset_at=reset)
    assert count('agy/zz-model-a') == 1
    assert 'skipped' not in by_id['zz-ok'] and by_id['zz-ok']['ok'] is False
    assert count('agy/zz-model-b') == 1

    # MCP forwards skipped rows unchanged.
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path / 'mcp')
    server.runner.leg_reader = store
    payload = asyncio.run(server.call_tool('plangraph_agent_probe', {'leg': 'zz-limited'}))
    assert payload == dict(ok=False, probes=[by_id['zz-limited']])


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


def test_echoed_tool_output_is_not_a_harness_limit():
    from plangraph.spawner.env_redact import harness_limit_line
    # run-26fc5542: a writer's own test source echoed in tool output marked mimo limited.
    echoed = "    store.record_harness_limit('agy/model', 'error: usage limit reached', 'spawner:harness', now=200)"
    for text in (echoed, echoed.strip(), '+' + echoed):
        assert harness_limit_line(1, text, '') is None
        assert harness_limit_line(1, '', text) is None
    assert harness_limit_line(0, '', 'error: usage limit reached') is None
    assert harness_limit_line(1, '', 'error: usage limit reached') == 'error: usage limit reached'
