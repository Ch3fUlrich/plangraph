import json
import subprocess
from pathlib import Path

import pytest

from plangraph.spawner import stall_watchdog as w
from conftest_watchdog import isolated_process_evidence  # noqa: F401
@pytest.mark.parametrize('bad', [{}, {'run_id': 'run-fixture', 'card': None},
                                {'run_id': 'run-fixture', 'card': 1}])
def test_bad_record_does_not_stop_tick(tmp_path, bad):
    directory = tmp_path / 'runs/run-fixture'
    directory.mkdir(parents=True)
    (directory / 'live.log').write_text('work')
    records = [dict(started=0, **bad), dict(run_id='run-later')]
    findings = w.tick(records, [], lambda _: None, now=0, state=tmp_path)
    assert [f['rule'] for f in findings] == ['unknown'] * (1 if bad.get('card', 1) is None else 2)


@pytest.mark.parametrize('error', [subprocess.CalledProcessError(1, 'bridge'), ValueError('invalid command')])
def test_flush_continues_after_poison(tmp_path, error):
    import subprocess
    out = tmp_path / 'watchdog/outbox'
    out.mkdir(parents=True)
    for name in ('a', 'b'):
        (out / (name + '.json')).write_text(json.dumps({'run_id': name}))
    def send(command, **kwargs):
        if json.loads(kwargs['input'])['run_id'] == 'a':
            raise error
    for count in (1, 2):
        health = w.flush_outbox(tmp_path, ['notify'], run=send)
        assert health == dict(failures=count, last_error=type(error).__name__)
    assert sorted(p.stem for p in out.iterdir()) == ['a']
    health = w.flush_outbox(tmp_path, ['notify'], run=lambda *a, **kw: None)
    assert health == dict(failures=0, last_error=None)
    assert json.loads((tmp_path / 'watchdog/delivery.json').read_text()) == health
    assert w.flush_outbox(tmp_path, ['notify'], run=send) == health

def test_unknown_reaches_outbox(tmp_path):
    findings = w.tick([dict(run_id='run-fixture')], [], lambda s: None,
                      state=tmp_path, deliver=lambda e: w.deliver_event(tmp_path, e))
    assert findings[0]['rule'] == 'unknown'
    event = json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())
    assert event['rule'] == 'unknown' and 'cancelled' not in event

def test_bridge_failure_status(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["bridge"]')
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(w, 'flush_outbox', lambda *a: dict(failures=2, last_error='OSError'))
    assert w.main(['tick']) == 1
    assert 'delivery-OSError failures=2' in (tmp_path / 'watchdog/status.txt').read_text()

@pytest.mark.parametrize('log_text', [None, 'first\nlatest\n'])
def test_unknown_has_full_shape(tmp_path, monkeypatch, log_text):
    import os
    directory = tmp_path / 'runs/run-fixture'
    directory.mkdir(parents=True)
    (directory / 'record.json').write_text('{')
    if log_text is not None:
        (directory / 'live.log').write_text(log_text)
        os.utime(directory / 'live.log', (900, 900))
    monkeypatch.setattr(w.time, 'time', lambda: 1000)
    assert w.sweep_runs(tmp_path) == []
    event = json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())
    expected = dict(run_id='run-fixture', rule='unknown', leg=None, role=None,
                    age=None, seconds_silent=100 if log_text is not None else None,
                    last_line='latest' if log_text is not None else None)
    assert event == dict(expected, kind='completion', event_id=event['event_id'])
    assert event['event_id']
    assert w.probe_run(tmp_path, dict(run_id='run-fixture'), 1000) == expected

def test_corrupt_record_skipped_but_unknown_delivered(tmp_path, monkeypatch):
    from plangraph.spawner.remote_probe import exchange, poll
    import subprocess
    path = tmp_path / 'runs/run-bad/record.json'
    path.parent.mkdir(parents=True)
    path.write_text('{')
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_PROBE_ROOT', 'fixture')
    assert w.sweep_runs(tmp_path) == []
    assert 'run-bad' in (tmp_path / 'watchdog/bad-records.json').read_text()
    events = exchange([])
    assert len(events) == 1
    event, = events.values()
    assert event['rule'] == 'unknown' and 'cancelled' not in event
    sent = []
    def run(argv, **kwargs):
        if argv[0] == 'ssh':
            return subprocess.CompletedProcess(argv, 0, json.dumps(exchange([])))
        sent.append(json.loads(kwargs['input']))
        if len(sent) == 1:
            raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(subprocess, 'run', run)
    poll(['fixture'], 'notify', tmp_path)
    assert w.sweep_runs(tmp_path) == []
    assert exchange([]) == events
    poll(['fixture'], 'notify', tmp_path)
    assert sent == [event, event]

def test_unknown_without_bridge_is_durable_not_tick_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [dict(run_id='run-fixture')])
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    assert w.main(['tick']) == 0
    event = json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())
    assert event['rule'] == 'unknown' and event['event_id']
    assert 'outbox 1, no bridge' in (tmp_path / 'watchdog/status.txt').read_text()
    assert 'delivery-bridge-unconfigured' in (tmp_path / 'watchdog/status.txt').read_text()
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    assert w.main(['tick']) == 0
    assert 'delivery-bridge-unconfigured' in (tmp_path / 'watchdog/status.txt').read_text()


@pytest.mark.parametrize('loaded', [{}, {'last_error': None}, {'failures': 2}])
def test_flush_partial_health_preserves_retry_and_health(tmp_path, loaded):
    w.deliver_event(tmp_path, dict(rule='unknown', run_id='fixture', kind='completion'))
    health_path = tmp_path / 'watchdog/delivery.json'
    health_path.write_text(json.dumps(loaded))
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    health = w.flush_outbox(tmp_path, ['notify'], run=fail)
    assert health == dict(failures=loaded.get('failures', 0) + 1, last_error='CalledProcessError')
    assert json.loads(health_path.read_text()) == health
    assert len(list((tmp_path / 'watchdog/outbox').glob('*.json'))) == 1
    assert w.flush_outbox(tmp_path, ['notify'], run=lambda *a, **kw: None) == dict(
        failures=0, last_error=None)


@pytest.mark.parametrize('age', [301, 5400])
def test_finished_child_redelivered_with_routing_alert(age):
    run = dict(run_id='run-abc', status='completed', ended=10000-age,
               started=1, exit_code=0, parent_session='parent')
    sent = []
    findings = w.tick([run], [], lambda session: 1, now=10000,
                      deliver=sent.append)
    assert [e['kind'] for e in sent] == ['completion', 'alert']
    assert sent[0]['recipient'] == 'parent'
    assert sent[0]['run_id'] == 'run-abc'
    assert sent[1]['recipient'] == 'routing'
    assert w.status(findings).startswith('! stall ')


def test_idle_claim_alerts_owner_and_routing():
    claim = dict(key='plangraph:STALL', state='claimed', claimed_by='owner',
                 session_id='parent', updated_at=1)
    sent = []
    assert w.tick([], [claim], lambda session: 1, now=2000, deliver=sent.append)
    assert [e['recipient'] for e in sent] == ['owner', 'routing']
    assert all(e['kind'] == 'alert' for e in sent)
    assert not w.tick([dict(status='running', item_key=claim['key'])],
                      [claim], lambda session: 1, now=2000)


@pytest.mark.parametrize('activity,ended', [(9900, 9400), (1, 9800), (None, 9400)])
def test_no_false_completion_alert(activity, ended):
    assert not w.tick([dict(run_id='run-a', status='completed', ended=ended,
                           parent_session='p')], [], lambda s: activity, now=10000)


def test_real_readers_and_durable_delivery(tmp_path):
    runs = tmp_path / 'runs/run-a'
    runs.mkdir(parents=True)
    (runs / 'record.json').write_text(json.dumps(dict(run_id='run-a')))
    assert w.read_runs(tmp_path) == [dict(run_id='run-a')]
    activity = tmp_path / 'session-activity/p'
    activity.parent.mkdir()
    activity.touch()
    assert w.read_activity(tmp_path, 'p') == activity.stat().st_mtime
    with pytest.raises(ValueError):
        w.read_activity(tmp_path, '../escape')
    w.deliver_event(tmp_path, dict(kind='completion', recipient='p', run_id='run-a'))
    assert json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())['recipient'] == 'p'
    class Keeper:
        def call(self, method, endpoint):
            assert (method, endpoint) == ('GET', 'snapshot')
            return {'items': [dict(key='plangraph:A', state='claimed')]}
    assert w.read_claims(Keeper())[0]['key'] == 'plangraph:A'


def test_delivery_bridge_acknowledges_only_success(tmp_path):
    w.deliver_event(tmp_path, dict(kind='alert', recipient='routing'))
    def fail(*args, **kwargs):
        raise RuntimeError('offline')
    with pytest.raises(RuntimeError):
        w.flush_outbox(tmp_path, ['bridge'], run=fail)
    assert list((tmp_path / 'watchdog/outbox').glob('*.json'))
    sent = []
    w.flush_outbox(tmp_path, ['bridge'], run=lambda args, **kw: sent.append(json.loads(kw['input'])))
    assert sent == [dict(kind='alert', recipient='routing')]
    assert not list((tmp_path / 'watchdog/outbox').glob('*.json'))


def test_tick_cli_persists_status_and_delivers_w12(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["bridge"]')
    monkeypatch.setattr(w.time, 'time', lambda: 10000)
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [dict(
        run_id='run-w12', status='completed', ended=4600, started=1,
        parent_session='parent', exit_code=0)])
    monkeypatch.setattr(w, 'read_activity', lambda state, session: 1)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    sent = []
    original = w.flush_outbox
    monkeypatch.setattr(w, 'flush_outbox', lambda state, command: original(
        state, command, run=lambda args, **kw: sent.append(json.loads(kw['input']))))
    assert w.main(['tick']) == 0
    assert sorted(e['kind'] for e in sent) == ['alert', 'completion']
    assert '! stall ' in (tmp_path / 'watchdog/status.txt').read_text()
    capsys.readouterr()
    w.main(['status'])
    assert capsys.readouterr().out.startswith('! stall ')


@pytest.mark.parametrize('host', ['linux', 'windows'])
def test_scheduler_installation(tmp_path, host, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv('PLANGRAPH_PROBE_HOSTS', raising=False)
    monkeypatch.delenv('PLANGRAPH_PROBE_NOTIFY_CMD', raising=False)
    calls = []
    w.install(host, tmp_path, python='/usr/bin/python3', root=Path.cwd(),
              run=lambda args, **kw: calls.append(args))
    content = '\n'.join(p.read_text(encoding='utf-16' if host == 'windows' else 'utf-8') for p in tmp_path.iterdir())
    assert 'plangraph.spawner.stall_watchdog' in content
    if host == 'linux':
        assert 'Environment="PLANGRAPH_PROBE_ROOT=' + str(Path.cwd()) + '"' in content
        unit = (tmp_path / 'plangraph-remote-probe.service').read_text()
        assert 'EnvironmentFile=-"' + str(tmp_path / 'config/plangraph/watchdog.env') + '"' in unit
        assert 'OnUnitActiveSec=120' in content
        assert ['systemctl', '--user', 'enable', '--now', 'plangraph-stall-watchdog.timer'] in calls
        assert ['systemctl', '--user', 'disable', '--now', 'plangraph-remote-probe.timer'] in calls
    else:
        assert '<Interval>PT2M</Interval>' in content
        assert calls[0][:2] == ['schtasks', '/Create']
        import re
        workdir = re.search(r'<WorkingDirectory>(.*)</WorkingDirectory>', content).group(1)
        assert (Path(workdir.replace('\\', '/')) / 'plangraph/spawner/stall_watchdog.py').is_file()
    bootstrap = Path('deploy/bootstrap.' + ('sh' if host == 'linux' else 'ps1')).read_text()
    assert 'plangraph.spawner.stall_watchdog' in bootstrap
    assert 'install' in bootstrap


@pytest.mark.parametrize('config_text', ['', 'PLANGRAPH_PROBE_HOSTS=fixture',
    'PLANGRAPH_PROBE_HOSTS=fixture\nPLANGRAPH_PROBE_NOTIFY_CMD=notify --json',
    '# comment\nbare-token\nPLANGRAPH_PROBE_HOSTS=fixture\nPLANGRAPH_PROBE_NOTIFY_CMD=notify --json=a',
    'PLANGRAPH_PROBE_HOSTS=fixture\nPLANGRAPH_PROBE_NOTIFY_CMD="notify --json"'])
def test_probe_timer_requires_service_config(tmp_path, monkeypatch, config_text):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_PROBE_HOSTS', raising=False)
    monkeypatch.delenv('PLANGRAPH_PROBE_NOTIFY_CMD', raising=False)
    config = tmp_path / 'plangraph/watchdog.env'
    config.parent.mkdir()
    config.write_text(config_text)
    calls = []
    w.install('linux', tmp_path, run=lambda args, **kw: calls.append(args))
    disabled = ['systemctl', '--user', 'disable', '--now', 'plangraph-remote-probe.timer']
    assert (disabled in calls) == ('PLANGRAPH_PROBE_NOTIFY_CMD' not in config_text)


@pytest.mark.parametrize('config_text', [None, 'PLANGRAPH_PROBE_HOSTS=fixture',
    'PLANGRAPH_PROBE_NOTIFY_CMD=""'])
def test_probe_timer_environment_fallback(tmp_path, monkeypatch, config_text):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_PROBE_HOSTS', 'fixture')
    monkeypatch.setenv('PLANGRAPH_PROBE_NOTIFY_CMD', 'notify')
    if config_text is not None:
        config = tmp_path / 'plangraph/watchdog.env'
        config.parent.mkdir()
        config.write_text(config_text)
    calls = []
    w.install('linux', tmp_path, run=lambda args, **kw: calls.append(args))
    assert any('disable' in call for call in calls)


def test_findings_without_bridge_report_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    def tick(*args, deliver, **kwargs):
        event = dict(rule='run-orphan', run_id='fixture', kind='completion')
        deliver(event)
        return [event]
    monkeypatch.setattr(w, 'tick', tick)
    assert w.main(['tick']) == 0
    assert 'delivery-bridge-unconfigured' in (tmp_path / 'watchdog/status.txt').read_text()
    assert list((tmp_path / 'watchdog/outbox').glob('*.json'))


@pytest.mark.parametrize('bridge', ['health', 'exception', 'unconfigured'])
def test_tick_status_keeps_error_and_finding(tmp_path, monkeypatch, bridge):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["bridge"]' if bridge != 'unconfigured' else '')
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    monkeypatch.setattr(w, 'tick', lambda *args, **kw: [dict(rule='run-orphan', run_id='fixture')])
    def fail():
        raise ValueError('fixture')
    monkeypatch.setattr(w, 'read_claims', fail)
    monkeypatch.setattr(w, 'status', lambda findings: '! stall run-orphan')
    def bridge_fail():
        raise RuntimeError('fixture')
    monkeypatch.setattr(w, 'flush_outbox', lambda *args: bridge_fail() if bridge == 'exception'
                        else dict(failures=1, last_error='CalledProcessError'))
    assert w.main(['tick']) == 1
    text = (tmp_path / 'watchdog/status.txt').read_text()
    assert '! stall run-orphan' in text and 'ValueError' in text
    assert 'delivery-' in text


def test_tick_keeper_unconfigured_skips_with_one_warning(tmp_path, monkeypatch):
    from plangraph.keeper.cli_verbs import MissingKeeperURL
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    monkeypatch.setattr(w, "sweep_runs", lambda state: [])
    monkeypatch.setattr(w, "read_claims", lambda: (_ for _ in ()).throw(MissingKeeperURL()))
    assert w.main(["tick"]) == 0
    assert (tmp_path / "watchdog/status.txt").read_text().count("keeper skipped MissingKeeperURL") == 1


def test_real_spawn_record_parent_and_item_come_from_completion(tmp_path):
    record = Path(__file__).parent / "fixtures/stall_real_spawn_record.json"
    (tmp_path / "runs/run-real").mkdir(parents=True)
    (tmp_path / "runs/run-real/record.json").write_text(record.read_text())
    [run] = w.read_runs(tmp_path)
    assert (run["parent_session"], run["item_key"]) == ("build-L1", "plangraph:STALL-WATCHDOG")
    assert w.tick([run], [], lambda s: 1, now=run["ended"] + 400)[0]["rule"] == "finished-child"


def test_unreadable_start_does_not_stop_classification(tmp_path, monkeypatch):
    import os
    directory = tmp_path / 'runs/run-fixture'
    directory.mkdir(parents=True)
    log = directory / 'live.log'
    log.write_text('work\n')
    os.utime(log, (100, 100))
    record = dict(run_id='run-fixture', status='running', pid=1, card={'stall_min': 1})
    def gone(*args):
        raise ProcessLookupError()
    for start in ({}, {'started': 'invalid'}, {'started': None}, {'started': float('nan')}):
        sample = dict(record, **start)
        monkeypatch.setattr(os, 'kill', gone)
        orphan = w.probe_run(tmp_path, sample, 160)
        assert (orphan['rule'], orphan['age']) == ('run-orphan', None)
        monkeypatch.setattr(os, 'kill', lambda *args: None)
        silent = w.probe_run(tmp_path, sample, 160)
        assert (silent['rule'], silent['age']) == ('run-silent', None)
        assert silent['seconds_silent'] == 60
        assert w.probe_run(tmp_path, sample, 159) is None


def test_host_run_probes(tmp_path, monkeypatch):
    import os
    from plangraph.spawner.stall_watchdog import tick
    from plangraph.spawner import runner
    directory = tmp_path / 'runs/run-fixture'
    directory.mkdir(parents=True)
    log = directory / 'live.log'
    log.write_text('work\n')
    record = dict(run_id='run-fixture', status='running', pid=os.getpid(),
                  started=0, role='implement', leg='fixture', card={'stall_min': 1})
    cancelled = []
    class Service:
        async def cancel(self, run_id):
            cancelled.append(run_id)
            return {'status': 'cancelled'}
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: Service())
    def check(now):
        return tick([record], [], lambda _: None, now=now, state=tmp_path)
    os.utime(log, (100, 100))
    assert check(159) == []
    assert check(160)[0]['rule'] == 'run-silent'
    assert check(160)[0]['episode'] == 100
    assert not cancelled
    assert check(220)[0]['cancelled']
    assert cancelled == ['run-fixture']
    os.utime(log, (220, 220))
    assert check(221) == []
    assert check(280)[0]['episode'] == 220
    log.unlink()
    assert check(500)[0]['rule'] == 'unknown'
    assert len(cancelled) == 1
    log.write_text('the retry is running in the background\n')
    record.update(status='completed', ended=230)
    assert check(500)[0]['rule'] == 'run-ended-no-commit'
    record.update(reason='writer_no_changes')
    log.write_text('done\n')
    assert check(500)[0]['rule'] == 'run-ended-no-commit'
    record.update(status='running')
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', gone)
    log.write_text('x' * 250 + '\n')
    os.utime(log, (220, 220))
    orphan = check(500)[0]
    assert (orphan['age'], orphan['seconds_silent'], orphan['last_line']) == (500, 280, 'x' * 200)
    log.unlink()
    assert check(500)[0]['rule'] == 'run-orphan'
    assert json.loads((directory / 'record.json').read_text())['reason'] == 'orphaned'


def test_seeded_silent_run_publishes_stall_episode(tmp_path, monkeypatch):
    import os
    from plangraph.spawner import stuck_watch
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(w.time, 'time', lambda: 161)
    monkeypatch.setattr(w.os, 'kill', lambda *args: None)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(stuck_watch, 'check', lambda *a, **k: [])
    directory = tmp_path / 'runs/run-seeded'
    directory.mkdir(parents=True)
    (directory / 'record.json').write_text(json.dumps(dict(
        run_id='run-seeded', status='running', pid=123, started=1,
        role='implement', card={'stall_min': 1})))
    log = directory / 'live.log'
    log.write_text('last progress\n')
    os.utime(log, (100, 100))
    assert w.main(['tick']) == 0
    alerts = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*.json')]
    assert len(alerts) == 1
    assert alerts[0]['kind'] == 'stall'
    assert alerts[0]['stalled_at'] == 160 and alerts[0]['noticed_at'] == 161
    assert (tmp_path / 'watchdog/status.txt').read_text().strip()
    assert w.main(['tick']) == 0
    assert len(list((tmp_path / 'watchdog/outbox').glob('*.json'))) == 1
