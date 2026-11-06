import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plangraph.spawner import host_ready as h

CONFIG = dict(ssh='invented-alias', repo='/invented/repo', spawner_src='/invented/source',
              env_file='/invented/env', run_cap=4, floor_mb=3000)


def runner(argv, **kwargs):
    script = argv[-1]
    assert argv[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    assert kwargs['timeout'] > 0
    output = ''
    if 'meminfo' in script:
        output = 'MemAvailable: 8000000 kB\n'
    elif 'df -Pk' in script:
        output = 'Filesystem 1024-blocks Used Available Capacity Mounted\nfake 9000000 0 9000000 0% /\n'
    elif 'rev-parse' in script:
        output = 'a' * 40
    elif 'doctor --json' in script:
        output = json.dumps(dict(ok=True, checks=[dict(name='keeper_whoami', status='pass')]))
    elif 'cli ps' in script:
        output = json.dumps(dict(runs=[dict(status='running'), dict(status='completed')]))
    return SimpleNamespace(returncode=0, stdout=output)


def test_green_atomic(tmp_path):
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert result['ok'] and result['reachable']
    assert result['doctor_ok'] and result['keeper_ok']
    assert result['live_runs'] == 1
    assert result['mem_available_mb'] == 7812
    assert result['disk_free_gb'] == 8
    assert result['spawner_session'] is None
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result
    assert (tmp_path / 'fiction.json').stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [tmp_path / 'fiction.json']
    assert h.fresh(result)
    assert not h.fresh(result, datetime.now(timezone.utc) + timedelta(minutes=11))


@pytest.mark.parametrize('payload', [{'ok': True}, {'checks': []}])
def test_doctor_missing_keys(tmp_path, payload):
    def fake(argv, **kwargs):
        if 'doctor --json' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        return runner(argv, **kwargs)
    result = h.check_host('fiction', CONFIG, tmp_path, fake)
    assert not result['ok']
    assert result['failures'] == ['doctor: invalid_response']
    assert result['doctor_ok'] is None and result['keeper_ok'] is None
    assert result['live_runs'] == 1
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result


def test_main_unknown_host(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hosts': {'fiction': CONFIG}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    with pytest.raises(SystemExit) as exc:
        h.main(['--host', 'nosuch'])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert 'error: unknown host' in captured.err
    assert 'Traceback' not in captured.err


@pytest.mark.parametrize('mode', ['unreachable', 'timeout', 'no_route', 'behind', 'doctor', 'fetch', 'session'])
def test_failures(tmp_path, mode):
    config = dict(CONFIG)
    if mode == 'no_route':
        config['ssh'] = None
    if mode == 'session':
        config['session_unit'] = 'fiction.service'
    def fake(argv, **kwargs):
        script = argv[-1]
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(argv, 1)
        if mode == 'unreachable' or (mode == 'fetch' and ' fetch ' in script) or (mode == 'session' and 'systemctl' in script):
            return SimpleNamespace(returncode=255, stdout='secret-value')
        if mode == 'behind' and 'origin/main' in script:
            return SimpleNamespace(returncode=0, stdout='b' * 40)
        if mode == 'doctor' and 'doctor --json' in script:
            return SimpleNamespace(returncode=1, stdout=json.dumps(dict(ok=False, checks=[
                dict(name='keeper_whoami', status='fail', detail='secret-value')])))
        return runner(argv, **kwargs)
    result = h.check_host('fiction', config, tmp_path, fake)
    assert not result['ok']
    text = (tmp_path / 'fiction.json').read_text()
    assert 'secret-value' not in text
    expected = dict(unreachable='reachable: command_failed', timeout='reachable: timeout',
                    no_route='reachable: no_route', behind='checkout: behind',
                    doctor='doctor.keeper_whoami: failed', fetch='fetch: command_failed',
                    session='session: command_failed')
    assert expected[mode] in result['failures']


def test_parallel_deadlines(tmp_path):
    import threading
    entered, finished = threading.Event(), threading.Event()
    def slow(argv, **kwargs):
        entered.set()
        assert finished.wait(2)
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
    with ThreadPoolExecutor(2) as pool:
        hung = pool.submit(h.check_host, 'slowfiction', CONFIG, tmp_path, slow)
        assert entered.wait(2)
        good = pool.submit(h.check_host, 'fastfiction', CONFIG, tmp_path, runner)
        try:
            assert good.result(timeout=2)['ok']
            assert (tmp_path / 'fastfiction.json').exists()
        finally:
            finished.set()
        assert not hung.result()['ok']


@pytest.mark.parametrize('session,probe', [
    ('myworker', 'pgrep -f -- myworker'),
    ('worker; secret-value', "pgrep -f -- 'worker; secret-value'"),
    ('fiction.service', 'systemctl --user is-active --quiet -- fiction.service'),
    ('fiction.scope', 'systemctl --user is-active --quiet -- fiction.scope'),
])
@pytest.mark.parametrize('live', [True, False])
def test_session_probe(tmp_path, session, probe, live):
    seen = []
    def fake(argv, **kwargs):
        if 'pgrep' in argv[-1] or 'systemctl' in argv[-1]:
            seen.append(argv[-1])
            return SimpleNamespace(returncode=0 if live and argv[-1] == probe else 1,
                                   stdout='secret-value')
        return runner(argv, **kwargs)
    result = h.check_host('fiction', dict(CONFIG, session_unit=session), tmp_path, fake)
    assert seen == [probe]
    assert result['spawner_session'] is live
    assert result['ok'] is live
    assert ('session: command_failed' in result['failures']) is not live
    assert 'secret-value' not in (tmp_path / 'fiction.json').read_text()


def test_state_write_failure(tmp_path, monkeypatch):
    def broken(*args):
        raise OSError('secret-value')
    monkeypatch.setattr(h, 'atomic_write_private', broken)
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert not result['ok']
    assert result['checked_at']
    assert result['failures'] == ['state: write_failed']
    assert 'secret-value' not in json.dumps(result)


@pytest.mark.parametrize('unwritable', [False, True])
def test_main_state_directory(tmp_path, monkeypatch, capsys, unwritable):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hosts': {'fiction': CONFIG, 'other': CONFIG}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(h.Path, 'home', lambda: tmp_path)
    state = tmp_path / '.local/state/plangraph/host-ready'
    original = h.check_host
    def check(name, cfg, directory):
        assert directory == state
        assert state.is_dir() or unwritable
        return original(name, cfg, directory, runner)
    monkeypatch.setattr(h, 'check_host', check)
    if unwritable:
        (tmp_path / '.local').write_text('blocked')
    assert h.main(['--json']) == int(unwritable)
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 2
    assert all(r['ok'] is not unwritable for r in records)
    if unwritable:
        assert all(r['failures'] == ['state: write_failed'] for r in records)
