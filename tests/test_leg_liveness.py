import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore


def test_down_expiry_and_recovery(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    assert store.last_probe_ts('agy/model') is None
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=100, down_s=20)
    state = store.leg_status('agy/model', now=110)
    assert state['state'] == 'reprobe'  # one failure never proves the leg is down
    assert state['evidence']['body_excerpt'] == 'timeout'
    assert store.last_probe_ts('agy/model') is None  # failures are not fresh evidence
    assert store.last_probe_attempt_ts('agy/model') == 100
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=111, down_s=20)
    assert store.leg_status('agy/model', now=115)['state'] == 'down'
    assert store.leg_status('agy/model', now=140)['state'] == 'reprobe'
    store.record_probe_pass('agy/model', 'leg-probe', now=141)
    assert store.leg_status('agy/model', now=141)['state'] == 'ok'
    assert store.last_probe_ts('agy/model') == 141


def test_probe_module_key_semantics():
    """Q16/M8: probe and quota bookkeeping share one leg key -- 'agy/<model>' only
    for an agy leg's bare model. Replaces the token `assert callable(probe)`."""
    from plangraph.spawner.leg_probe import key
    assert key(SimpleNamespace(harness='agy', model='gemini-3.8-flash')) == 'agy/gemini-3.8-flash'
    assert key(SimpleNamespace(harness='agy', model='prov/model')) == 'prov/model'
    assert key(SimpleNamespace(harness='qodercli', model='Qwen3.8-Flash')) == 'Qwen3.8-Flash'
    assert key(SimpleNamespace(model='agy/model')) == 'agy/model'   # legacy rows stay stable


@pytest.mark.parametrize('success', [True, False])
def test_commit_probe_and_timeout(monkeypatch, success):
    from plangraph.spawner.leg_probe import probe, key
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.routing_support import leg_state
    from dataclasses import replace
    leg = replace(next(l for l in DEFAULT_LEGS if l.harness == 'agy'), roles=('implement',))
    roots = []
    async def factory(*args, **kwargs):
        roots.append(kwargs['cwd'])
        if success:
            code = "from pathlib import Path; import subprocess; Path('ok.txt').write_text('ok'); subprocess.run(['git','add','ok.txt'],check=True); subprocess.run(['git','-c','commit.gpgsign=false','commit','-m','probe'],check=True)"
        else:
            code = 'import time; time.sleep(60)'
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)
    runner = SpawnerRunner(process_factory=factory)
    result = asyncio.run(probe(runner, leg, timeout_s=3 if success else .3))
    assert result['ok'] is success
    assert result['reason'] == ('' if success else 'timeout')
    assert 'state' not in result
    assert leg_state.leg_status(key(leg))['state'] == ('ok' if success else 'reprobe')
    assert roots and not Path(roots[0]).exists()
    if not success:
        leg_state.record_probe_fail(key(leg), 'timeout', 'leg-probe')  # second consecutive failure
        assert leg_state.leg_status(key(leg))['state'] == 'down'
        from plangraph.planner.routing import plan_spawn
        plan = plan_spawn({'estimate_tokens': 1000}, role='implement', legs=(leg,), catalog_rows=())
        assert not plan.legs
        assert plan.skipped[0]['reason'] == 'down'


def test_ttl_and_writer_only(monkeypatch):
    from plangraph.spawner import leg_probe
    leg = SimpleNamespace(model='agy/model', harness='agy')
    calls = []
    async def fake(*args, **kwargs):
        calls.append(args)
        return dict(ok=False, reason='timeout', seconds=0)
    monkeypatch.setattr(leg_probe, 'probe', fake)
    async def run():
        assert await leg_probe.before_launch(None, leg, 'implement', False) is None
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
        assert await leg_probe.before_launch(None, leg, 'review', False) is None
        assert await leg_probe.before_launch(None, leg, 'implement', True) is None
        assert await leg_probe.before_launch(None, leg, 'implement', False) == 'down: timeout'
        leg_probe.leg_state.record_probe_pass('agy/model', 'leg-probe')
        assert await leg_probe.before_launch(None, leg, 'implement', False) is None
    asyncio.run(run())
    assert len(calls) == 1


def test_execute_probe_falls_through(tmp_path, monkeypatch):
    from mechanics_legs import install_fallthrough_legs
    install_fallthrough_legs(monkeypatch)
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner, spawn_service, JobInfo
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    service = spawn_service(runner, state=tmp_path / 'state')
    probed, launched = [], []
    async def fake_probe(runner, leg, **kwargs):
        probed.append(leg.id)
        return dict(ok=len(probed) > 1, reason='timeout', seconds=0)
    async def spawn(**kwargs):
        launched.append(kwargs['model_or_combo'])
        return JobInfo(job_id='fake', status='completed', task_prompt='',
                       model_or_combo=kwargs['model_or_combo'], exit_code=0)
    monkeypatch.setattr(leg_probe, 'probe', fake_probe)
    monkeypatch.setattr(runner, 'spawn', spawn)
    record = asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='work', wait_seconds=None))
    assert record['status'] == 'completed'
    assert len(probed) == 2 and len(launched) == 1
    assert record['attempts'][0]['status'] == 'skipped'
    assert record['attempts'][0]['skip_reason'] == 'down: timeout'


@pytest.fixture(params=['cx-gpt-6.1-sol', 'cx-gpt-6.1-sol-xhigh'])
def failed_probe(tmp_path, monkeypatch, request):
    from plangraph.spawner import leg_probe
    from plangraph.planner.legs import spawnable_legs
    leg = next(l for l in spawnable_legs() if l.id == request.param)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = SimpleNamespace(leg_reader=store, stderr='probe diagnostic', jobs={})
    from plangraph.spawner.runner import spawn_service
    spawn_service(runner, state=tmp_path, leg_reader=store)
    async def spawn(**kwargs):
        return SimpleNamespace(job_id='probe', status='failed', exit_code=1, stderr=runner.stderr)
    async def cancel(job_id):
        pass
    def temporary(**kwargs):
        root = tmp_path / 'probe'
        root.mkdir()
        return str(root)
    runner.spawn, runner.cancel = spawn, cancel
    monkeypatch.setattr(leg_probe.time, 'time', lambda: 1000)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **kw: 'fake')
    monkeypatch.setattr(leg_probe, 'caller_heads', lambda: {})
    monkeypatch.setattr(leg_probe.tempfile, 'mkdtemp', temporary)
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **kw: b'initial')
    monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', '1800')
    return runner, leg, store


def test_probe_stderr_excerpt_and_output(failed_probe, monkeypatch, capsys):
    from plangraph.spawner import cli, leg_probe
    from plangraph.spawner.env_redact import redact, redact_env_values
    runner, leg, store = failed_probe
    monkeypatch.setenv('PROBE_TEST_TOKEN', 'synthetic-probe-credential')
    runner.stderr = 'discarded\n' + 'detail ' * 90 + '\nBearer sk-example\nsynthetic-probe-credential\nfinal failure'
    from plangraph.routing_support.leg_state import _redact
    expected = ' '.join(_redact(redact(redact_env_values(runner.stderr, os.environ)), limit=None)[-400:].splitlines())
    result = asyncio.run(leg_probe.probe_many(runner, [leg.id]))['probes'][0]
    assert result['stderr_excerpt'] == expected and len(expected) == 400
    assert expected.endswith('[REDACTED] [autoos:redacted] final failure')
    assert 'discarded' not in result['reason'] and expected in result['reason']
    assert 'synthetic-probe-credential' not in result['reason'] and 'sk-example' not in result['reason']
    assert '[REDACTED]' in result['reason']  # the mask is evidence of scrubbing, not a credential
    with store._connect() as db:
        assert db.execute('SELECT body_excerpt FROM leg_events').fetchone()[0] == result['reason']
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=runner))
    assert cli.main(['probe', '--leg', leg.id]) == 1
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize('age,status,model,text,serving', [
    (119, 'running', None, 'writing', True), (120, 'running', None, 'writing', True),
    (121, 'running', None, 'writing', False), (None, 'running', None, 'writing', False),
    (0, 'completed', None, 'writing', False), (0, 'running', 'other/model', 'writing', False),
    (0, 'running', None, '', False),
])
def test_probe_failure_live_run(failed_probe, monkeypatch, age, status, model, text, serving):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    if age is not None:
        directory = runner.state / 'runs' / 'run-live'
        directory.mkdir(parents=True)
        current = dict(asdict(leg), model=model or leg.model)
        (directory / 'record.json').write_text(json.dumps(dict(
            status=status, chosen_leg=None, attempts=[], plan=dict(legs=[current]))))
        log = directory / 'live.log'
        log.write_text(text)
        os.utime(log, (1000 - age, 1000 - age))
    store.record_probe_fail(leg_probe.key(leg), 'earlier failure', 'leg-probe', now=999)
    result = asyncio.run(leg_probe.probe_many(runner, [leg.id]))['probes'][0]
    assert result['ok'] is False
    with store._connect() as db:
        event = dict(db.execute('SELECT * FROM leg_events ORDER BY id DESC').fetchone())
    assert 'probe diagnostic' in event['body_excerpt']
    assert event['source'] == ('probe_failed_leg_serving' if serving else 'liveness-probe')
    assert event['reset_at'] == (None if serving else 2800)
    assert event['retry_after_s'] == (None if serving else 1800)
    assert store.leg_status(leg_probe.key(leg))['state'] == ('ok' if serving else 'down')
    assert store.last_probe_ts(leg_probe.key(leg)) is None
    assert store.last_probe_attempt_ts(leg_probe.key(leg)) == 1000
    assert store.last_probe_outcome(leg_probe.key(leg))['ok'] is False
    if serving:
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
        assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
        store.record_probe_fail(leg_probe.key(leg), 'later failure', 'leg-probe')
        assert store.leg_status(leg_probe.key(leg))['state'] == 'reprobe'


def test_serving_probe_does_not_clear_quota(failed_probe):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe, exit_outcome
    runner, leg, store = failed_probe
    directory = runner.state / 'runs' / 'run-live'
    directory.mkdir(parents=True)
    (directory / 'record.json').write_text(json.dumps(dict(status='running', chosen_leg=asdict(leg))))
    log = directory / 'live.log'
    log.write_text('writing')
    os.utime(log, (1000, 1000))
    job = SimpleNamespace(stdout='', stderr='Error: usage limit reached', exit_code=1,
                          status='failed', events=[], command=['opencode'])
    record = {}
    assert exit_outcome.fallthrough_reason(SimpleNamespace(leg_reader=store), leg, job, record)
    assert record['reason'] == 'harness_limit'
    result = asyncio.run(leg_probe.probe(runner, leg, store=store))
    assert result['state'] == 'probe_failed_leg_serving'
    assert store.leg_status(leg_probe.key(leg))['state'] == 'limited'
    store.record_probe_fail(leg_probe.key(leg), 'later probe failure', 'leg-probe')
    assert store.leg_status(leg_probe.key(leg))['state'] == 'limited'


def test_probe_serving_uses_current_attempt(failed_probe):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    directory = runner.state / 'runs' / 'run-live'
    directory.mkdir(parents=True)
    previous = dict(asdict(leg), id='other', model='other/model')
    (directory / 'record.json').write_text(json.dumps(dict(status='running', chosen_leg=None,
        plan=dict(legs=[previous, asdict(leg)]), attempts=[dict(leg=previous, status='failed')])))
    log = directory / 'live.log'
    log.write_text('writing')
    os.utime(log, (1000, 1000))
    assert leg_probe._leg_serving(runner, leg)


@pytest.mark.parametrize('serving', [False, True])
def test_probe_failure_legacy_excerpt_bound(tmp_path, serving):
    from plangraph.routing_support.leg_state import _redact
    store = LegStateStore(tmp_path / 'legs.sqlite')
    reason = 'detail ' * 100 + 'Bearer sk-example'
    store.record_probe_fail('agy/model', reason, 'test', serving=serving)
    with store._connect() as db:
        assert db.execute('SELECT body_excerpt FROM leg_events').fetchone()[0] == _redact(reason)


def test_probe_output_omits_state_without_serving(failed_probe):
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    result = asyncio.run(leg_probe.probe_many(runner, [leg.id]))['probes'][0]
    assert not result['ok']
    assert 'state' not in result


@pytest.mark.parametrize('explicit_state', [False, True])
def test_probe_does_not_scan_environment_state(failed_probe, monkeypatch, tmp_path, explicit_state):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    directory = tmp_path / 'foreign' / 'runs' / 'run-live'
    directory.mkdir(parents=True)
    (directory / 'record.json').write_text(json.dumps(dict(status='running', chosen_leg=asdict(leg))))
    log = directory / 'live.log'
    log.write_text('writing')
    os.utime(log, (1000, 1000))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(directory.parents[1]))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'foreign-xdg'))
    if not explicit_state:
        del runner.state
    assert not leg_probe._leg_serving(runner, leg)


@pytest.mark.parametrize('change,serving', [('touch', False), ('append', True), ('stale_append', False), ('truncate', False), ('replace', False), ('finish', False)])
def test_probe_touched_log_is_not_growth(failed_probe, change, serving):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    directory = runner.state / 'runs' / 'run-live'
    directory.mkdir(parents=True)
    (directory / 'record.json').write_text(json.dumps(dict(status='running', chosen_leg=asdict(leg))))
    log = directory / 'live.log'
    log.write_text('old output')
    os.utime(log, (800, 800))
    spawn = runner.spawn
    async def touch(**kwargs):
        if change in ('append', 'stale_append'):
            with log.open('a') as stream:
                stream.write(' new output')
        elif change == 'truncate':
            log.write_text('x')
        elif change == 'replace':
            replacement = directory / 'replacement'
            replacement.write_text('new output in another file')
            replacement.replace(log)
        elif change == 'finish':
            (directory / 'record.json').write_text(json.dumps(dict(status='completed', chosen_leg=asdict(leg))))
        stamp = 879 if change == 'stale_append' else 1000
        os.utime(log, (stamp, stamp))
        return await spawn(**kwargs)
    runner.spawn = touch
    store.record_probe_fail(leg_probe.key(leg), 'earlier failure', 'test', now=999)
    result = asyncio.run(leg_probe.probe(runner, leg, store=store))
    if serving:
        assert result['state'] == 'probe_failed_leg_serving'
    else:
        assert 'state' not in result
    assert store.leg_status(leg_probe.key(leg))['state'] == ('ok' if serving else 'down')


@pytest.mark.parametrize('shape', ['default', 'positional', 'keyword', 'plain'])
def test_dispatch_service_state_wiring(tmp_path, shape):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    runner = object() if shape == 'plain' else SpawnerRunner()
    if shape == 'default':
        service = spawn_service(state=tmp_path)
    elif shape == 'positional':
        service = spawn_service(runner, state=tmp_path)
    else:
        service = spawn_service(runner=runner, state=tmp_path)
    assert service.state == tmp_path
    if shape != 'plain':
        assert service.runner.state == tmp_path
    else:
        assert service.runner is runner


@pytest.mark.parametrize('race', ['record_removed', 'record_changed', 'log_removed', 'log_replaced', 'malformed'])
def test_probe_run_snapshot_races_fail_closed(failed_probe, monkeypatch, race):
    from dataclasses import asdict
    from plangraph.spawner import leg_probe
    runner, leg, store = failed_probe
    directory = runner.state / 'runs' / 'run-live'
    directory.mkdir(parents=True)
    record = directory / 'record.json'
    record.write_text(json.dumps(dict(status='running', chosen_leg=asdict(leg))))
    log = directory / 'live.log'
    log.write_text('writing')
    os.utime(log, (1000, 1000))
    original = Path.read_text
    def read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path == record:
            if race == 'record_removed':
                record.unlink()
            elif race == 'record_changed':
                record.write_text(json.dumps(dict(status='completed', chosen_leg=asdict(leg))))
            elif race == 'log_removed':
                log.unlink()
            elif race == 'log_replaced':
                replacement = directory / 'replacement'
                replacement.write_text('')
                replacement.replace(log)
            else:
                return '{invalid'
        return text
    monkeypatch.setattr(Path, 'read_text', read)
    assert not leg_probe._leg_serving(runner, leg)


@pytest.mark.parametrize('serving', [False, True])
def test_probe_event_explicit_stderr_bound(tmp_path, serving):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_fail('agy/model', 'reason ' * 100, 'test', serving=serving,
                            stderr_excerpt='Bearer sk-example ' + 'diagnostic ' * 100)
    with store._connect() as db:
        excerpt = db.execute('SELECT body_excerpt FROM leg_events').fetchone()[0]
    assert len(excerpt) == 702  # 300 reason + separator + 400 stderr
    assert excerpt[300:312] == ': [REDACTED]'
    assert 'sk-example' not in excerpt
