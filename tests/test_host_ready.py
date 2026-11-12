import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plangraph.spawner import host_ready as h
from plangraph.spawner import host_ready_source as source

CONFIG = dict(ssh='invented-alias', repo='/invented/repo', spawner_src='/invented/source',
              env_file='/invented/env', run_cap=4, floor_mb=3000, session_unit='worker')


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    directory = tmp_path / 'plangraph'
    directory.mkdir()
    (directory / 'ps-hosts.json').write_text(json.dumps({'hosts': [
        dict(name=name, target=CONFIG['ssh'], src='/invented/source')
        for name in ('fiction', 'other', 'slowfiction', 'fastfiction')]}))


def runner(argv, **kwargs):
    script = argv[-1]
    assert argv[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    assert kwargs['timeout'] > 0
    output = ''
    if 'echo src_missing' in script:
        output = 'ok'
    elif 'rev-list' in script:
        output = '0'
    elif 'meminfo' in script:
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
    assert result['spawner_session'] is True
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result
    assert (tmp_path / 'fiction.json').stat().st_mode & 0o777 == 0o600
    assert set(tmp_path.iterdir()) == {tmp_path / 'fiction.json', tmp_path / 'plangraph'}
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
    assert result['failures'] == ([] if live else ['session: command_failed'])
    assert 'secret-value' not in (tmp_path / 'fiction.json').read_text()


@pytest.mark.parametrize('error', [OSError, UnicodeError, RuntimeError])
def test_state_write_failure(tmp_path, monkeypatch, error):
    def broken(*args):
        raise error('secret-value')
    monkeypatch.setattr(h, 'atomic_write_private', broken)
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert not result['ok']
    assert result['checked_at']
    assert result['failures'] == ['state: write_failed']
    assert 'secret-value' not in json.dumps(result)


def test_session_not_configured(tmp_path):
    config = dict(CONFIG)
    del config['session_unit']
    result = h.check_host('fiction', config, tmp_path, runner)
    assert not result['ok']
    assert result['spawner_session'] is None
    assert result['failures'] == ['session: not_configured']


def test_config_error_before_probe(tmp_path, monkeypatch, capsys):
    config = dict(CONFIG)
    del config['ssh']
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'hosts': {'fiction': config}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(path))
    monkeypatch.setattr(h.Path, 'home', lambda: tmp_path)
    def never_contact(*args, **kwargs):
        pytest.fail('host must not be contacted')
    original = h.check_host
    monkeypatch.setattr(h, 'check_host', lambda name, cfg, state:
                        original(name, cfg, state, never_contact))
    assert h.main(['--json']) == 1
    output = capsys.readouterr()
    assert output.err == 'HOST-NOT-READY fiction config invalid_response_or_config\n'
    assert json.loads(output.out)[0]['reachable'] is False


@pytest.mark.parametrize('ps_config', ['{', json.dumps({'hosts': [dict(
    name='fiction', target='invented-alias', win_src='C:/source')]})])
def test_deleted_spawner_with_invalid_ps_config(tmp_path, ps_config):
    directory = tmp_path / 'plangraph'
    directory.mkdir(exist_ok=True)
    (directory / 'ps-hosts.json').write_text(ps_config)
    probes = []
    def fake(argv, **kwargs):
        if 'echo src_missing' in argv[-1]:
            probes.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout='src_missing')
        return runner(argv, **kwargs)
    result = h.check_host('fiction', CONFIG, tmp_path, fake)
    assert not result['ok'] and result['src_ok'] is False
    assert len(probes) == 1 and '/invented/source' in probes[0]
    assert 'src_missing: failed' in result['failures']
    assert any(f.startswith('src_config: ') for f in result['failures'])


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


@pytest.mark.parametrize('src', [123, True, 1.5, ['/source'], {'path': '/source'}, None, ''])
def test_invalid_source_type(tmp_path, monkeypatch, src):
    monkeypatch.setattr(source, 'configured_hosts', lambda: [
        dict(name='fiction', target=CONFIG['ssh'], src=src)])
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert not result['ok'] and result['src_ok'] is False
    assert any(f.startswith('src_config: ') for f in result['failures'])
    assert result['doctor_ok'] and result['live_runs'] == 1


@pytest.mark.parametrize('error', [AttributeError, OverflowError, RuntimeError, RecursionError])
def test_main_unexpected_host_errors(tmp_path, monkeypatch, capsys, error):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'hosts': {'fiction': CONFIG, 'other': CONFIG}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(path))
    monkeypatch.setattr(h.Path, 'home', lambda: tmp_path)
    def broken(*args, **kwargs):
        raise error('secret-value')
    original = h.check_host
    monkeypatch.setattr(h, 'check_host', lambda name, cfg, state:
                        original(name, cfg, state, broken))
    assert h.main(['--json']) == 1
    output = capsys.readouterr()
    records = json.loads(output.out)
    assert len(records) == 2 and all(not r['ok'] for r in records)
    assert 'secret-value' not in output.out + output.err
    for record in records:
        assert (f"HOST-NOT-READY {record['host']} config invalid_response_or_config\n"
                in output.err)
        state = tmp_path / '.local/state/plangraph/host-ready' / (record['host'] + '.json')
        assert json.loads(state.read_text()) == record
