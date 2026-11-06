import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import stall_watchdog as w


def records(state, count=1, **fields):
    for i in range(count):
        path = state / f'runs/run-{i}/record.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(run_id=f'run-{i}', **fields)))
    return w.read_runs(state, dry_run=True)


def test_old_history(tmp_path):
    runs = records(tmp_path, 1000, status='failed', started=0, ended=1, pid=None)
    assert w.tick(runs, [], lambda _: None, now=600000, state=tmp_path) == []


@pytest.mark.parametrize('ended,expected', [(96400, 1), (10000, 0), (None, 0),
                                          (100001, 0), ('bad', 0), (float('nan'), 0)])
def test_finished_horizon(tmp_path, ended, expected):
    runs = records(tmp_path, status='failed', role='implement',
                   reason='writer_no_changes', ended=ended)
    findings = w.tick(runs, [], lambda _: None, now=100000, state=tmp_path)
    assert [f['rule'] for f in findings] == ['run-ended-no-commit'] * expected


def test_live_evidence(tmp_path, monkeypatch):
    runs = records(tmp_path, status='running', started=0, pid=123)
    monkeypatch.setattr(os, 'kill', lambda *args: None)
    assert w.probe_run(tmp_path, runs[0], 600000)['rule'] == 'unknown'
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', gone)
    assert w.probe_run(tmp_path, runs[0], 600000)['rule'] == 'run-orphan'


@pytest.mark.parametrize('age', [30, 719, 720, 1440, None])
@pytest.mark.parametrize('log_kind', ['missing', 'unreadable'])
def test_no_log_startup_grace(tmp_path, monkeypatch, age, log_kind):
    from plangraph.spawner import runner
    runs = records(tmp_path, status='running', started=None if age is None else 2000 - age,
                   pid=os.getpid(), card={'stall_min': 12})
    if log_kind == 'unreadable':
        (tmp_path / 'runs/run-0/live.log').mkdir()
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: pytest.fail('cancel attempted'))
    sent = []
    findings = w.tick(runs, [], lambda _: None, now=2000, state=tmp_path,
                      deliver=lambda e: w.deliver_event(tmp_path, e))
    w.flush_outbox(tmp_path, ['notify'], run=lambda cmd, **kw: sent.append(json.loads(kw['input'])))
    if age is not None and age < 720:
        assert findings == sent == []
    else:
        assert findings[0]['rule'] == sent[0]['rule'] == 'unknown'
        assert sent[0]['event_id'] and 'cancelled' not in sent[0]


@pytest.mark.parametrize('value,age,expected', [
    ('999999999', 604800, 1), ('999999999', 604801, 0),
    ('bad', 86400, 1), ('bad', 86401, 0)])
def test_horizon_setting(tmp_path, monkeypatch, value, age, expected):
    monkeypatch.setenv('PLANGRAPH_PROBE_HORIZON_S', value)
    runs = records(tmp_path, status='failed', role='implement',
                   reason='writer_no_changes', ended=1000000 - age)
    assert len(w.tick(runs, [], lambda _: None, now=1000000, state=tmp_path)) == expected


def test_bad_cap_survives_restart(tmp_path, monkeypatch):
    records(tmp_path, 30, status='running', started=0, pid=None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_PROBE_MAX_PER_TICK', 'bad')
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    script = '''
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
sys.modules['plangraph.spawner.runner'] = SimpleNamespace(
    spawn_service=lambda **kw: SimpleNamespace(retry_completions=lambda: None))
from plangraph.spawner import stall_watchdog as w
w.read_claims = lambda: []
raise SystemExit(w.main(['tick']))
'''
    first = None
    for expected in (25, 30):
        result = subprocess.run([sys.executable, '-S', '-c', script,
                                 str(Path(w.__file__).resolve().parents[2])], cwd=tmp_path,
                                env=dict(os.environ, PYTHONPATH=''), capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        events = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*')]
        findings = {e['run_id']: e for e in events if e['rule'] == 'unknown'}
        assert len(findings) == expected
        assert sum(e['rule'] == 'findings-capped' for e in events) == 1
        if first is not None:
            assert all(findings[key] == event for key, event in first.items())
        first = findings


@pytest.mark.parametrize('cap', ['0', '0.5'])
def test_small_cap_makes_progress(tmp_path, monkeypatch, cap):
    from plangraph.spawner import runner
    records(tmp_path, 3, status='running', started=0, pid=None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_PROBE_MAX_PER_TICK', cap)
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: type(
        'Service', (), {'retry_completions': lambda self: None})())
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    for expected in (1, 2):
        assert w.main(['tick']) == 0
        events = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*')]
        assert sum(e['rule'] == 'unknown' for e in events) == expected


def test_cap_window_replay(tmp_path, monkeypatch):
    from plangraph.spawner import runner
    records(tmp_path, 4, status='running', started=0, pid=None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_PROBE_MAX_PER_TICK', '1')
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["notify"]')
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: type(
        'Service', (), {'retry_completions': lambda self: None})())
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    sent = []
    flush = w.flush_outbox
    real_clock = time.time
    def blocked(command, **kwargs):
        assert time.time is real_clock
        sent.append(json.loads(kwargs['input']))
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(w, 'flush_outbox', lambda state, command: flush(state, command, run=blocked))
    for now in (1201, 1202, 1320):
        monkeypatch.setattr(w, 'time', SimpleNamespace(time=lambda: now))
        assert w.main(['tick']) == 1
        caps = [e for e in sent if e['rule'] == 'findings-capped']
        assert len({e['event_id'] for e in caps}) == (2 if now == 1320 else 1)
        if now != 1201:
            assert caps[0] == caps[1]


def test_dry_run_and_cap(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import runner, stuck_watch
    records(tmp_path, 30, status='running', started=0, pid=123)
    monkeypatch.setattr(os, 'kill', lambda *args: None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: pytest.fail('service called'))
    (tmp_path / 'runs/run-bad').mkdir()
    (tmp_path / 'runs/run-bad/record.json').write_text('{')
    log = tmp_path / 'runs/run-0/live.log'
    log.write_text('quiet tool')
    os.utime(log, (1, 1))
    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    before = snapshot()
    with monkeypatch.context() as dry:
        dry.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["notify"]')
        dry.setattr(w, 'flush_outbox', lambda *a, **kw: pytest.fail('notify called'))
        dry.setattr(w, 'deliver_event', lambda *a, **kw: pytest.fail('mailbox called'))
        from plangraph.spawner import private_io
        dry.setattr(private_io, 'atomic_write_private',
                    lambda *a, **kw: pytest.fail('state write called'))
        # Guard the cached import too, without letting a lazy import retain the
        # temporary guard after this context (as in the Postgres CI selection).
        dry.setattr(stuck_watch, 'atomic_write_private', private_io.atomic_write_private)
        class DryService:
            def retry_completions(self):
                pytest.fail('retry_completions called')
        dry.setattr(runner, 'spawn_service', lambda **kw: DryService())
        assert w.main(['tick', '--dry-run']) == 0
    assert not (tmp_path / 'watchdog/outbox').exists()
    assert not (tmp_path / 'watchdog/alerts').exists()
    assert not (tmp_path / 'watchdog/status.txt').exists()
    assert snapshot() == before
    output = capsys.readouterr().out
    assert 'unknown: 30' in output and 'run-silent: 1' in output
    (tmp_path / 'runs/run-bad/record.json').unlink()
    log.unlink()
    class Service:
        def retry_completions(self):
            pass
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: Service())
    for expected in (26, 31):
        assert w.main(['tick']) == 0
        events = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*')]
        assert len(events) == expected
        assert sum(e['rule'] == 'findings-capped' for e in events) == 1


def test_pull_host_keeps_findings(tmp_path, monkeypatch):
    from plangraph.spawner import runner
    from plangraph.keeper.cli_verbs import MissingKeeperURL
    records(tmp_path, status='running', started=0, pid=123)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(os, 'kill', lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: SimpleNamespace(retry_completions=lambda: None))
    def unavailable():
        raise MissingKeeperURL()
    monkeypatch.setattr(w, 'read_claims', unavailable)
    assert w.main(['tick']) == 0
    events = list((tmp_path / 'watchdog/outbox').glob('*.json'))
    assert len(events) == 1 and json.loads(events[0].read_text())['rule'] == 'run-orphan'
    text = (tmp_path / 'watchdog/status.txt').read_text()
    assert 'outbox 1, no bridge' in text and 'warning keeper skipped MissingKeeperURL' in text
