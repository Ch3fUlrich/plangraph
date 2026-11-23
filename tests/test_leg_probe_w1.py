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

@pytest.fixture(autouse=True)
def no_head_trailer_lookup(monkeypatch):
    """These cards name placeholder repositories: the head Writer trailer lookup is not under test."""
    monkeypatch.setattr("plangraph.planner.seats.head_writer_families", lambda card: [])


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
    result = asyncio.run(leg_probe.probe(runner, SimpleNamespace(model='gemini-4-neon-medium', harness='agy'), timeout_s=.3, store=store))
    assert result['ok'] is False
    assert result['reason'] == 'timeout' and not result['infra']
    assert store.leg_status('agy/gemini-4-neon-medium')['state'] == 'reprobe'
    assert leg_state.leg_status('agy/gemini-4-neon-medium')['state'] == 'ok'


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

    caller = tmp_path / 'caller'
    caller.mkdir()
    env = leg_probe.probe_env()
    subprocess.run(['git', 'init', '-q'], cwd=caller, env=env, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'initial', '--allow-empty'], cwd=caller, env=env, check=True)
    monkeypatch.chdir(caller)
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
    leg = SimpleNamespace(model='gemini-4-neon-medium', harness='agy')
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
    assert str(nothing.value) == 'select --leg ID, --all-writers or --all-reviewers'
    with pytest.raises(ParameterRefused) as unknown:
        asyncio.run(probe_many(None, 'zz/nope', False))
    assert str(unknown.value) == 'unknown legs: zz/nope'


def test_probe_many_skips_a_limited_leg(tmp_path, monkeypatch):
    """D-3685: probe_many skips a leg booked limited and writes no row for it;
    an unbooked leg still probes."""
    from plangraph.planner import legs as planner_legs
    from plangraph.spawner import leg_probe

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


def test_review_probe_selects_routed_legs_and_excludes_failed_from_route(tmp_path, monkeypatch):
    from plangraph.planner.legs import REVIEW_ORDER
    from plangraph.planner.routing import plan_spawn
    from plangraph.routing_support import leg_state, resolver
    from plangraph.spawner import leg_probe
    from plangraph.spawner.service import SpawnService

    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda name, **kwargs: None if name == 'pg-spawnd' else '/bin/' + name)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    # Argon efforts share one agy quota key (D-5966): a failing argon probes down both.
    failing = {'gemini-4-argon-low', 'gemini-4-argon-medium'}
    seen = []

    class Runner:
        state = tmp_path
        leg_reader = store
        _tasks = {}
        jobs = {}

        async def spawn(self, **kwargs):
            seen.append((kwargs['model_or_combo'], kwargs['role'], kwargs['prompt']))
            fail = kwargs['model_or_combo'] in failing
            return SimpleNamespace(
                job_id=f"j-{len(seen)}",
                status='failed' if fail else 'completed',
                exit_code=1 if fail else 0,
                stdout='' if fail else 'OK',
                stderr='simulated review failure' if fail else '',
                events=[{'event': 'opencode_reasoning_exhausted'}] if fail else [],
            )

        async def cancel(self, job_id):
            pass

    runner = Runner()
    first = asyncio.run(leg_probe.probe_many(runner, all_reviewers=True, timeout=5))
    assert first['ok'] is False
    assert [row['id'] for row in first['probes']] == list(REVIEW_ORDER)
    assert all(role == 'review' and prompt == leg_probe.REVIEW_PROMPT for _, role, prompt in seen)
    assert store.leg_status('agy/gemini-4-argon-low')['state'] == 'down'

    card = {'estimate_tokens': 1000, 'diff_ref': {'repo': str(tmp_path), 'base': 'a', 'head': 'b'}}
    plan = plan_spawn(card, role='review', writer_families=('openai',), leg_reader=store, catalog_rows=())
    assert not {'agy-gemini-4-argon-low', 'agy-gemini-4-argon-medium'} & {leg.id for leg in plan.legs}
    assert plan.legs[0].id == 'agy-gemini-4-hydrogen-high'
    assert any(s['leg'] == 'agy-gemini-4-argon-low' and s['reason'] == 'down' for s in plan.skipped)

    # Shared LegAdapter must honor the stored down probe even when the gateway snapshot is healthy.
    provider = SimpleNamespace(snapshot=lambda: {
        'opencode': SimpleNamespace(available=True, stale=False),
        'mimo': SimpleNamespace(available=True, stale=False),
        'omniroute': SimpleNamespace(available=True, stale=False),
        'console-deepseek': SimpleNamespace(available=True, stale=False),
        'opencode-go': SimpleNamespace(available=True, stale=False),
    })
    from plangraph.planner.legs import routed_review_legs
    served = tuple(leg.model for leg in routed_review_legs())
    adapter = leg_state.LegAdapter(store, provider, lambda: served, enabled=True, clock=lambda: 0)
    svc = SpawnService(runner=runner, state=tmp_path, leg_reader=adapter)
    routed = svc.route(card, role='review', writer_families=('openai',), benchmark={})
    assert not {'agy-gemini-4-argon-low', 'agy-gemini-4-argon-medium'} & {leg.id for leg in routed.legs}
    assert routed.legs[0].id == 'agy-gemini-4-hydrogen-high'

    failing.clear()
    recovered = asyncio.run(leg_probe.probe_many(runner, all_reviewers=True, timeout=5))
    assert recovered['ok'] is True
    assert store.leg_status('agy/gemini-4-argon-low')['state'] == 'ok'
    after = svc.route(card, role='review', writer_families=('openai',), benchmark={})
    assert after.legs[0].id == 'agy-gemini-4-hydrogen-high'
    assert 'agy-gemini-4-argon-low' in [leg.id for leg in after.legs]


def test_cli_probe_all_reviewers(tmp_path, monkeypatch, capsys):
    import json
    from plangraph.spawner import cli, leg_probe
    calls = []

    async def fake(runner, legs, all_writers, timeout, *, all_reviewers=False):
        calls.append((legs, all_writers, timeout, all_reviewers))
        return dict(ok=True, probes=[dict(id='mimo-v2.6-pro', ok=True, reason='', seconds=0.1)])

    monkeypatch.setattr(leg_probe, 'probe_many', fake)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=SimpleNamespace()))
    assert cli.main(['--state', str(tmp_path), 'probe', '--all-reviewers', '--json']) == 0
    assert calls == [([], False, None, True)]
    assert json.loads(capsys.readouterr().out)['ok'] is True


def test_review_probe_flags_caller_repo_leak(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda name, **kwargs: '/bin/' + name)
    heads = iter([{'cwd': 'a', 'env': 'a'}, {'cwd': 'b', 'env': 'a'}])
    monkeypatch.setattr(leg_probe, 'caller_heads', lambda: next(heads))

    class Runner:
        state = tmp_path
        leg_reader = LegStateStore(tmp_path / 'legs.sqlite')
        _tasks = {}
        jobs = {}

        async def spawn(self, **kwargs):
            return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout='OK', stderr='', events=[])

        async def cancel(self, job_id):
            pass

    leg = SimpleNamespace(model='mimo/mimo-v2.6-pro', harness='opencode', roles=('review',))
    out = asyncio.run(leg_probe.probe(Runner(), leg, timeout_s=5, role='review'))
    assert out['ok'] is False
    assert out['reason'] == 'probe leaked into caller repo'


def test_review_probe_coverage_equals_review_route(tmp_path, monkeypatch):
    from plangraph.planner.legs import REVIEW_ORDER, routed_review_legs
    from plangraph.planner.routing import plan_spawn
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    card = {'estimate_tokens': 1000, 'diff_ref': {'repo': str(tmp_path), 'base': 'a', 'head': 'b'}}
    plan = plan_spawn(card, role='review', writer_families=('openai',), leg_reader=LegStateStore(tmp_path / 'l.sqlite'), catalog_rows=())
    assert [leg.id for leg in plan.legs] == [leg.id for leg in routed_review_legs() if leg.family not in {'gpt', 'claude'}]
    assert [leg.id for leg in routed_review_legs()] == list(REVIEW_ORDER)


def test_named_writer_leg_with_all_reviewers_keeps_writer_probe(tmp_path, monkeypatch):
    from plangraph.planner.legs import DEFAULT_LEGS, REVIEW_ORDER, leg_active
    from plangraph.spawner import leg_probe
    writer = next(leg for leg in DEFAULT_LEGS if 'implement' in leg.roles and leg_active(leg) and leg.id not in REVIEW_ORDER)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    roles = {}

    async def fake_probe(runner, leg, *, timeout_s=None, store=None, role=None):
        roles[leg.id] = role
        if leg.id == writer.id:
            store.record_probe_fail(leg_probe.key(leg), 'boom', 'leg-probe')
        return dict(ok=leg.id != writer.id, reason='' if leg.id != writer.id else 'boom', seconds=0.0)

    monkeypatch.setattr(leg_probe, 'probe', fake_probe)
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    runner = SimpleNamespace(leg_reader=store, state=tmp_path)
    asyncio.run(leg_probe.probe_many(runner, [writer.id], all_reviewers=True, timeout=5))
    assert roles[writer.id] is None
    assert all(roles[leg_id] == 'review' for leg_id in roles if leg_id != writer.id) and len(roles) > 1
    assert store.leg_status(leg_probe.key(writer))['state'] != 'down'


def test_probe_budget_inside_a_spawn_is_below_the_admission_window(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.delenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', raising=False)
    budget = leg_probe.ProbeBudget()
    assert budget.timeout() == base_fresh.ADMISSION_TIMEOUT / 3
    budget.spend(time.monotonic() - 70)  # one probe used 70 s of the spawn's 90 s
    assert budget.timeout() == pytest.approx(base_fresh.ADMISSION_TIMEOUT / 2 - 70, abs=0.5)
    budget.spend(time.monotonic() - 30)
    assert budget.timeout() == 0
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', '5')
    assert leg_probe.ProbeBudget().timeout() == 5


def test_hung_probes_fail_over_leg_by_leg_inside_the_admission_window(monkeypatch):
    """Serial hung probes share one budget: failover, then a refusal, never base_admission_timeout."""
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', '180')
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', 1.5)  # 0.5 s per probe, 0.75 s in all
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda name, **kwargs: '/bin/true')

    class Runner:
        def __init__(self):
            self._tasks, self.jobs, self.cancelled = {}, {}, []

        async def spawn(self, **kwargs):
            job = SimpleNamespace(job_id='hung%d' % len(self.jobs), status='running', exit_code=None)
            self.jobs[job.job_id] = job
            self._tasks[job.job_id] = asyncio.ensure_future(asyncio.sleep(3600))  # the harness hangs
            return job

        async def cancel(self, job_id):
            self.cancelled.append(job_id)
            self.jobs[job_id].status = 'cancelled'
            self._tasks[job_id].cancel()

    runner = Runner()
    legs = [SimpleNamespace(model='agy/model%d' % n, harness='agy') for n in range(3)]

    async def run():
        started, budget, skips = time.monotonic(), leg_probe.ProbeBudget(), []
        for leg in legs:  # what the runner does for each leg it tries
            skips.append(await asyncio.wait_for(
                leg_probe.before_launch(runner, leg, 'implement', False, budget),
                base_fresh.ADMISSION_TIMEOUT))
        return skips, time.monotonic() - started

    skips, elapsed = asyncio.run(run())
    assert skips == ['down: timeout', 'down: timeout', 'probe_budget_exhausted']
    assert elapsed < base_fresh.ADMISSION_TIMEOUT
    assert runner.cancelled == ['hung0', 'hung1']  # hung harnesses are killed, not orphaned


def test_a_spawn_joining_an_inflight_probe_is_bounded_and_charged(monkeypatch):
    """The joining spawn waits at most its own remaining budget, and that wait is charged."""
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', 0.9)  # 0.3 s per probe, 0.45 s in all

    async def slow(runner, leg, **kwargs):
        await asyncio.sleep(5)  # another spawn's probe, still running
        return dict(ok=True, reason='', seconds=0)

    monkeypatch.setattr(leg_probe, 'probe', slow)

    async def run():
        owner = asyncio.ensure_future(leg_probe.before_launch(None, _leg(), 'implement', False))
        await asyncio.sleep(0.05)  # the owner's probe is in flight
        budget, started, skips = leg_probe.ProbeBudget(), time.monotonic(), []
        for _ in range(3):  # three legs joining the same in-flight probe
            skips.append(await leg_probe.before_launch(None, _leg(), 'implement', False, budget))
        waited = time.monotonic() - started
        owner.cancel()
        return skips, waited

    skips, waited = asyncio.run(run())
    assert skips == ['probe_budget_exhausted'] * 3
    assert waited < 0.6  # 0.3 + 0.15 of budget, then nothing: not 3 x 0.3


def test_the_spawn_that_starts_a_probe_is_bounded_too(monkeypatch):
    """A probe stuck in setup or cleanup cannot hold its own spawn past the probe budget."""
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', 0.9)  # 0.3 s per probe

    async def stuck(runner, leg, **kwargs):
        await asyncio.sleep(5)  # e.g. delayed log scanning before its own deadline starts
        return dict(ok=True, reason='', seconds=0)

    monkeypatch.setattr(leg_probe, 'probe', stuck)

    async def run():
        budget, started = leg_probe.ProbeBudget(), time.monotonic()
        skip = await leg_probe.before_launch(None, _leg(), 'implement', False, budget)
        return skip, time.monotonic() - started, budget.left

    skip, waited, left = asyncio.run(run())
    assert skip == 'probe_budget_exhausted'
    assert waited < 0.5 and left < 0.45  # 0.3 s probe + a short grace, charged to the budget


def test_probe_budget_is_net_of_the_admission_time_already_spent(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.delenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', raising=False)
    window = base_fresh.ADMISSION_TIMEOUT
    assert leg_probe.ProbeBudget().left == window / 2
    late = leg_probe.ProbeBudget(started=time.time() - (window - window / 6 - 10))  # 10 s left before the reserve
    assert late.timeout() == pytest.approx(10, abs=0.5)
    spent = leg_probe.ProbeBudget(started=time.time() - window)
    assert spent.timeout() == 0


def test_a_backward_wall_clock_step_does_not_restore_probe_budget(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    window = base_fresh.ADMISSION_TIMEOUT
    token = base_fresh.ADMISSION_STARTED.set(time.monotonic() - (window - window / 6 - 10))
    try:
        # The wall clock jumped back an hour: only the monotonic admission clock is trusted.
        monkeypatch.setattr(leg_probe.time, 'time', lambda: 1_000_000.0)
        budget = leg_probe.ProbeBudget(started=1_000_000.0 + 3600)
        assert budget.timeout() == pytest.approx(10, abs=0.5)
    finally:
        base_fresh.ADMISSION_STARTED.reset(token)

