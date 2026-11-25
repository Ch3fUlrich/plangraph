import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from registry_route_util import use_projected_overflow_config

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
    elif 'pg-host-readiness' in script:
        output = json.dumps(dict(errors=[], sandbox_ok=True, sandbox_error=None))
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


def test_readiness_checks_configured_worktree_disk(tmp_path):
    root = '/mnt/work disk'

    def fake(argv, **kwargs):
        if 'df -Pk' in argv[-1] and root in argv[-1]:
            return SimpleNamespace(returncode=0, stdout='header\nfake 9000000 7000000 2000000 80% /mnt/work\n')
        return runner(argv, **kwargs)

    result = h.check_host('fiction', dict(CONFIG, work_mounts=[root]), tmp_path, fake)
    assert not result['ok']
    assert result['work_mounts'] == {root: 1}
    assert result['failures'] == [f'disk: disk_low: {root}: free 1 GB']


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
    use_projected_overflow_config(monkeypatch, config)
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
    ('myworker', 'pgrep -x -- myworker'),
    ('worker; secret-value', "pgrep -x -- 'worker; secret-value'"),
    ('/run/worker.pid', 'plangraph wait-pid --check --pid-file /run/worker.pid'),
    ('fiction.service', 'systemctl --user is-active --quiet -- fiction.service'),
    ('fiction.scope', 'systemctl --user is-active --quiet -- fiction.scope'),
])
@pytest.mark.parametrize('live', [True, False])
def test_session_probe(tmp_path, session, probe, live):
    seen = []
    def fake(argv, **kwargs):
        if any(cmd in argv[-1] for cmd in ('pgrep', 'systemctl', 'wait-pid')):
            assert 'pgrep -f' not in argv[-1]
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
    use_projected_overflow_config(monkeypatch, path)
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
    reason = 'unreadable' if ps_config == '{' else 'unsupported_or_invalid'
    assert result['failures'] == [f'src_config: {reason}', 'src_missing: failed']


@pytest.mark.parametrize('unwritable', [False, True])
def test_main_state_directory(tmp_path, monkeypatch, capsys, unwritable):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hosts': {'fiction': CONFIG, 'other': CONFIG}}))
    use_projected_overflow_config(monkeypatch, config)
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
    assert result['failures'] == ['src_config: route_or_path_unknown']
    assert result['doctor_ok'] and result['live_runs'] == 1


@pytest.mark.parametrize('error', [AttributeError, OverflowError, RuntimeError])
def test_main_unexpected_host_errors(tmp_path, monkeypatch, capsys, error):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'hosts': {'fiction': CONFIG, 'other': CONFIG}}))
    use_projected_overflow_config(monkeypatch, path)
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


@pytest.mark.parametrize('error', [OSError, FileNotFoundError, ValueError, KeyError, TypeError])
def test_unreadable_ps_config_has_one_reason(tmp_path, monkeypatch, error):
    def broken():
        raise error('secret-value')
    monkeypatch.setattr(source, 'configured_hosts', broken)
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert not result['ok'] and result['src_ok'] is False
    assert result['failures'] == ['src_config: unreadable']
    assert result['doctor_ok'] and result['live_runs'] == 1


@pytest.mark.parametrize('host,reason', [
    (dict(src=None), 'route_or_path_unknown'),
    (dict(src=['/source']), 'route_or_path_unknown'),
    (dict(src={'path': '/source'}), 'route_or_path_unknown'),
    (dict(target='other-alias', src='/source'),
     "ps target='other-alias' and overflow ssh='invented-alias' must be the same string"),
    (dict(win_src='C:/source'), 'unsupported_or_invalid'),
    (dict(invalid=True), 'unsupported_or_invalid'),
])
def test_duplicate_invalid_ps_sources(tmp_path, monkeypatch, host, reason):
    host = dict(dict(name='fiction', target=CONFIG['ssh']), **host)
    monkeypatch.setattr(source, 'configured_hosts', lambda: [host, dict(host)])
    result = h.check_host('fiction', CONFIG, tmp_path, runner)
    assert not result['ok'] and result['src_ok'] is False
    assert result['failures'] == [f'src_config: {reason}']
    assert result['doctor_ok'] and result['live_runs'] == 1


def test_duplicate_valid_ps_sources_are_probed_once(tmp_path, monkeypatch):
    host = dict(name='fiction', target=CONFIG['ssh'], src='/invented/source')
    monkeypatch.setattr(source, 'configured_hosts', lambda: [host, dict(host)])
    probes = []
    def fake(argv, **kwargs):
        if 'echo src_missing' in argv[-1]:
            probes.append(argv[-1])
        return runner(argv, **kwargs)
    result = h.check_host('fiction', CONFIG, tmp_path, fake)
    assert result['ok'] and result['failures'] == []
    assert len(probes) == 2  # One checkout import and one remote-ps import.


@pytest.mark.parametrize('error', [MemoryError, RecursionError, KeyboardInterrupt,
                                  SystemExit, GeneratorExit])
@pytest.mark.parametrize('stage', ['probe', 'sources', 'state'])
def test_fatal_host_errors_propagate(tmp_path, monkeypatch, error, stage):
    def broken(*args, **kwargs):
        raise error('secret-value')
    if stage == 'sources':
        monkeypatch.setattr(source, 'configured_hosts', broken)
    elif stage == 'state':
        monkeypatch.setattr(h, 'atomic_write_private', broken)
    with pytest.raises(error, match='secret-value'):
        h.check_host('fiction', CONFIG, tmp_path, broken if stage == 'probe' else runner)
    assert not (tmp_path / 'fiction.json').exists()


@pytest.mark.parametrize('floor,free,ok', [(None, 4, False), (None, 5, True),
                                         (10, 9, False), (10, 10, True)])
def test_remote_disk_floor(tmp_path, floor, free, ok):
    config = dict(CONFIG)
    if floor is not None:
        config['disk_floor_gb'] = floor
    def fake(argv, **kwargs):
        if 'df -Pk' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=f'fake 99999999 0 {free * 1024 * 1024} 0% /\n')
        return runner(argv, **kwargs)
    result = h.check_host('fiction', config, tmp_path, fake)
    assert result['ok'] is ok
    assert result['disk_free_gb'] == free
    assert result['failures'] == ([] if ok else [f'disk: disk_low: free {free} GB'])


@pytest.mark.parametrize('d1,d2,ok', [(2, 9, True), (9, 2, True), (2, 2, False)])
def test_remote_work_drives_are_probed_per_drive_and_one_eligible_drive_is_ready(tmp_path, d1, d2, ok):
    config = dict(CONFIG, work_drives=[dict(path='/invented/d1'), dict(path='/invented/d2', disk_floor_gb=3)])
    def fake(argv, **kwargs):
        script = argv[-1]
        for path, free in (('/invented/d1', d1), ('/invented/d2', d2)):
            if 'df -Pk' in script and path in script:
                return SimpleNamespace(returncode=0, stdout=f'fake 99999999 0 {free * 1024 * 1024} 0% /\n')
        return runner(argv, **kwargs)
    result = h.check_host('fiction', config, tmp_path, fake)
    assert result['work_drives'] == [
        dict(path='/invented/d1', disk_floor_gb=5, disk_free_gb=d1),
        dict(path='/invented/d2', disk_floor_gb=3, disk_free_gb=d2)]
    assert result['ok'] is ok


@pytest.mark.parametrize('missing', [[], ['pytest'], ['xdist'], ['pytest', 'xdist']])
def test_registry_host_reports_missing_pytest_and_xdist(tmp_path, missing):
    scripts = []

    def fake(argv, **kwargs):
        script = argv[-1]
        scripts.append(script)
        if '--test-tools' in script:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(missing=missing, umask='0022', unit_umask=None)))
        if 'remote_ps' in script:
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(runs=[])))
        return runner(argv, **kwargs)

    result = h.check_host('fiction', dict(CONFIG, registry=True), tmp_path, fake)
    probe = next(s for s in scripts if '--test-tools' in s)
    assert 'plangraph.spawner.host_ready --test-tools /invented/repo' in probe and '--unit' not in probe
    assert result['python_ok'] is (not missing)
    assert result['ok'] is (not missing), result['failures']
    assert [f for f in result['failures'] if f.startswith('python:')] == (
        [f'python: missing {" ".join(missing)}'] if missing else [])


@pytest.mark.parametrize('session,queried', [('spawner.service', True), ('spawner.pid', False), ('', False)])
def test_registry_host_queries_the_unit_umask_of_its_service_by_name(tmp_path, session, queried):
    scripts = []

    def fake(argv, **kwargs):
        scripts.append(argv[-1])
        if '--test-tools' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(missing=[], umask='0022', unit_umask='0002' if queried else None)))
        if 'remote_ps' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(runs=[])))
        return runner(argv, **kwargs)

    result = h.check_host('fiction', dict(CONFIG, registry=True, session_unit=session), tmp_path, fake)
    probe = next(s for s in scripts if '--test-tools' in s)
    assert ('--unit spawner.service' in probe) is queried
    assert ('umask: unit umask 0002 leaves new files group- or other-writable' in result['failures']) is queried


def shim(directory, name='shim', fails='xdist'):
    path = directory / name
    path.write_text('#!/bin/sh\n[ "$2" = "import %s" ] && exit 1\nexit 0\n' % fails)
    path.chmod(0o755)
    return path


def test_report_follows_the_hook_interpreter_choice(tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.venv/bin').mkdir(parents=True)
    shim(repo / '.venv/bin', 'python', fails='xdist')
    other = shim(tmp_path, 'configured', fails='pytest')
    # The checkout venv wins over PREFLIGHT_PYTHON: only the venv shim lacks xdist.
    assert h.pre_push_report(str(repo), environ={'PREFLIGHT_PYTHON': str(other)})['missing'] == ['xdist']
    (repo / '.venv/bin/python').unlink()
    assert h.pre_push_report(str(repo), environ={'PREFLIGHT_PYTHON': str(other)})['missing'] == ['pytest']
    assert h.pre_push_report(str(repo), environ={'PREFLIGHT_PYTHON': str(tmp_path / 'absent')})['missing'] == [
        'pytest', 'xdist']


def test_report_searches_a_relative_path_entry_from_the_checkout(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'bin').mkdir(parents=True)
    shim(repo / 'bin', 'python3')
    assert h.pre_push_report(str(repo), environ={'PATH': 'bin:/nonexistent'})['missing'] == ['xdist']


@pytest.mark.parametrize('value', ['./shim', 'shim'])
def test_report_resolves_relative_preflight_python_in_the_checkout(tmp_path, value):
    repo = tmp_path / 'repo'
    repo.mkdir()
    shim(repo)
    assert h.pre_push_report(str(repo), environ={'PREFLIGHT_PYTHON': value})['missing'] == ['xdist']
    assert h.pre_push_report(str(repo), environ={'PREFLIGHT_PYTHON': 'not-there'})['missing'] == [
        'pytest', 'xdist']


@pytest.mark.parametrize('form', ['~/hook-python', '~someone/hook-python'])
def test_report_expands_home_and_named_user_preflight_python(tmp_path, monkeypatch, form):
    import pwd
    home = tmp_path / 'home'
    home.mkdir()
    shim(home, 'hook-python')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_dir=str(home)))
    assert h.pre_push_report(str(tmp_path), environ={'PREFLIGHT_PYTHON': form})['missing'] == ['xdist']


@pytest.mark.parametrize('mask', [0o022, 0o002, 0o020])
def test_report_carries_this_processes_umask(tmp_path, mask):
    old = os.umask(mask)
    try:
        report = h.pre_push_report(str(tmp_path), environ={})
    finally:
        os.umask(old)
    assert report['umask'] == format(mask, '04o') and report['unit_umask'] is None


def test_report_asks_systemd_for_the_named_user_unit(tmp_path, monkeypatch):
    seen = []
    def fake_run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout='0002\n') if argv[0] == 'systemctl' else real_run(argv, **kwargs)
    real_run = subprocess.run
    monkeypatch.setattr(h.subprocess, 'run', fake_run)
    report = h.pre_push_report(str(tmp_path), unit='spawner.service', environ={})
    assert report['unit_umask'] == '0002'
    assert ['systemctl', '--user', 'show', '--property=UMask', '--value', 'spawner.service'] in seen


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('systemctl', 10), OSError('no systemctl')])
def test_unit_umask_that_cannot_be_read_is_a_failure(tmp_path, monkeypatch, failure):
    real_run = subprocess.run
    def fake_run(argv, **kwargs):
        if argv[0] == 'systemctl':
            raise failure
        return real_run(argv, **kwargs)
    monkeypatch.setattr(h.subprocess, 'run', fake_run)
    failures = []
    ok = h.python_test_tools(
        lambda check, command, parse: parse(json.dumps(h.pre_push_report(str(tmp_path), 'spawner.service', {}))),
        lambda check, reason: failures.append(f'{check}: {reason}'), 'prefix ', str(tmp_path), 'spawner.service')
    assert 'umask: unit umask of spawner.service unavailable' in failures


def test_cli_with_a_relative_checkout_argument_resolves_path_entries_in_that_checkout(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'checkout'
    (repo / 'bin').mkdir(parents=True)
    shim(repo / 'bin', 'python3')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', 'bin:/nonexistent')
    monkeypatch.delenv('PREFLIGHT_PYTHON', raising=False)
    assert h.main(['--test-tools', 'checkout']) == 0
    assert json.loads(capsys.readouterr().out)['missing'] == ['xdist']


def test_unit_umask_from_a_failed_systemctl_query_is_not_accepted(tmp_path, monkeypatch):
    real_run = subprocess.run
    def fake_run(argv, **kwargs):
        if argv[0] == 'systemctl':
            return SimpleNamespace(returncode=1, stdout='0022\n')
        return real_run(argv, **kwargs)
    monkeypatch.setattr(h.subprocess, 'run', fake_run)
    assert h.pre_push_report(str(tmp_path), 'spawner.service', {})['unit_umask'] is None


def plan_runner(calls, src_code=0, missing=()):
    """A runner that answers `pg-spawnd steps` plans like `runner` answers shell scripts."""
    def run(argv, **kwargs):
        plan = json.loads(kwargs['input'])
        calls.append((argv, plan))
        steps = plan['steps']
        if steps[0].get('fail_code') == 10:
            return SimpleNamespace(returncode=src_code, stdout='ok\n')
        text = ' '.join(steps[0].get('argv', []))
        if 'plangraph.spawner.remote_ps' in text:
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(runs=[])))
        if '--test-tools' in text:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(missing=list(missing), umask='0022', unit_umask=None)))
        return runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--', 'x', text], **kwargs)
    return run


def test_local_registry_host_runs_every_probe_as_a_step_plan_without_a_shell(tmp_path):
    calls = []
    config = dict(CONFIG, local=True, registry=True, env_file='/invented/env', state_dir='/invented/state')
    result = h.check_host('fiction', config, tmp_path, plan_runner(calls))
    assert result['ok'], result['failures']
    assert calls and all(argv == ['pg-spawnd', 'steps'] for argv, _ in calls)
    spawner_plans = [plan for _, plan in calls if plan.get('cwd') == '/invented/source']
    assert spawner_plans and all(plan['env_file'] == '/invented/env' for plan in spawner_plans)
    assert all(plan['steps'][0]['env'] == {'PYTHONPATH': 'src', 'PLANGRAPH_SPAWN_STATE': '/invented/state'}
               for plan in spawner_plans)
    tools = next(plan for _, plan in calls if plan['steps'][0]['argv'][1:] == ['--version'])
    assert [step['argv'][0] for step in tools['steps']] == ['git', 'python3', 'codex']


@pytest.mark.parametrize('code,failure', [(10, 'src_missing: failed'), (11, 'src_import: failed')])
def test_source_probe_plan_reports_missing_and_unimportable_sources_by_exit_code(tmp_path, code, failure):
    calls = []
    config = dict(CONFIG, local=True, registry=True)
    result = h.check_host('fiction', config, tmp_path, plan_runner(calls, src_code=code))
    assert failure in result['failures']
    src = next(plan for _, plan in calls if plan['steps'][0].get('fail_code') == 10)
    assert [step.get('fail_code') for step in src['steps']] == [10, 11, None]
    assert src['steps'][1]['cwd'] == '/' and src['steps'][1]['argv'][-1] == 'import plangraph'


def test_wsl_registry_host_probes_travel_as_plans_over_ssh(tmp_path):
    seen = []
    def fake(argv, **kwargs):
        seen.append((argv, kwargs.get('input')))
        return plan_runner([], missing=())(argv, **kwargs) if 'input' in kwargs else runner(argv, **kwargs)
    (tmp_path / 'plangraph' / 'ps-hosts.json').write_text(json.dumps({'hosts': [
        dict(name='fiction', target=CONFIG['ssh'], src='/invented/source', wsl_distro='Ubuntu')]}))
    config = dict(CONFIG, registry=True, wsl_distro='Ubuntu')
    result = h.check_host('fiction', config, tmp_path, fake)
    assert result['ok'], result['failures']
    assert seen and all(argv[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
                        and argv[-1] == 'wsl.exe --distribution Ubuntu --exec pg-spawnd steps'
                        and json.loads(body)['steps'] for argv, body in seen)


@pytest.mark.parametrize('wsl', [False, True])
def test_checkout_path_with_shell_characters_stays_literal_in_every_probe_plan(tmp_path, wsl):
    repo = '/invented/check out (review)'
    calls = []
    if wsl:
        (tmp_path / 'plangraph' / 'ps-hosts.json').write_text(json.dumps({'hosts': [
            dict(name='fiction', target=CONFIG['ssh'], src='/invented/source', wsl_distro='Ubuntu')]}))
        config = dict(CONFIG, registry=True, wsl_distro='Ubuntu', repo=repo)
        def fake(argv, **kwargs):
            return plan_runner(calls, **{})(argv, **kwargs)
        runner_ = fake
    else:
        config = dict(CONFIG, local=True, registry=True, repo=repo)
        runner_ = plan_runner(calls)
    result = h.check_host('fiction', config, tmp_path, runner_)
    assert result['ok'], result['failures']
    git_plans = [plan for _, plan in calls if plan['steps'][0]['argv'][:2] == ['git', '-C']]
    assert git_plans and all(plan['steps'][0]['argv'][2] == repo for plan in git_plans)
    df = next(plan for _, plan in calls if plan['steps'][0]['argv'][:1] == ['df'])
    assert df['steps'][0]['argv'][-1] == repo


@pytest.mark.parametrize('available,ok', [(2999, False), (3000, True)])
def test_remote_memory_floor_with_legacy_run_cap(tmp_path, available, ok):
    def fake(argv, **kwargs):
        if 'meminfo' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=f'MemAvailable: {available * 1024} kB\n')
        if 'cli ps' in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {'runs': [{'status': 'running'}] * CONFIG['run_cap']}))
        return runner(argv, **kwargs)

    result = h.check_host('fiction', CONFIG, tmp_path, fake)
    assert result['ok'] is ok
    assert result['mem_available_mb'] == available
    assert result['live_runs'] == CONFIG['run_cap']
    assert result['failures'] == ([] if ok else ['memory: below_floor'])
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result


@pytest.mark.parametrize('command,output,field,check', [
    ('meminfo', 'MemFree: 8000000 kB', 'mem_available_mb', 'memory'),
    ('df -Pk', '', 'disk_free_gb', 'disk'),
    ('rev-parse HEAD', 'bad-sha', 'checkout_sha', 'checkout'),
    ('rev-parse origin/main', 'bad-sha', 'main_sha', 'main'),
    ('rev-list', '-1', 'behind_main', 'behind_main'),
    ('cli ps', '{', 'live_runs', 'runs'),
    ('cli ps', '{"runs": null}', 'live_runs', 'runs'),
    ('cli ps', '{"runs": {}}', 'live_runs', 'runs'),
    ('cli ps', '{"runs": [{}]}', 'live_runs', 'runs'),
    ('cli ps', '{"runs": [{"status": false}]}', 'live_runs', 'runs'),
])
def test_invalid_probe_response(tmp_path, command, output, field, check):
    def fake(argv, **kwargs):
        if command in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=output)
        return runner(argv, **kwargs)

    result = h.check_host('fiction', CONFIG, tmp_path, fake)
    assert result['reachable'] and not result['ok']
    assert result[field] is None
    assert result['failures'] == [f'{check}: invalid_response']
    assert result['doctor_ok'] and result['spawner_session']
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result


def test_host_record_keeps_native_sandbox_failure(tmp_path):
    error = 'sandbox_unavailable: bwrap: setting up uid map: Permission denied'
    planned = plan_runner([])

    def fake(argv, **kwargs):
        first = json.loads(kwargs['input'])['steps'][0].get('argv', [])
        if first and first[0] == 'pg-host-readiness':
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(errors=[], sandbox_ok=False, sandbox_error=error)))
        return planned(argv, **kwargs)

    result = h.check_host('fiction', dict(CONFIG, local=True, registry=True), tmp_path, fake)
    assert result['ok'] and result['trust_ok']
    assert result['sandbox_ok'] is False
    assert result['sandbox_error'] == error
    assert json.loads((tmp_path / 'fiction.json').read_text()) == result


def test_local_record_carries_served_legs_and_main_prints_the_digest(tmp_path, monkeypatch, capsys):
    from plangraph.routing_support import leg_state
    from plangraph.routing_support.leg_state import DiscoveryFailed
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models',
                        staticmethod(lambda: DiscoveryFailed('managed_service_port_conflict')))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hosts': {'fiction': dict(CONFIG, local=True, registry=True)}}))
    use_projected_overflow_config(monkeypatch, config)
    monkeypatch.setattr(h.Path, 'home', lambda: tmp_path)
    original = h.check_host
    monkeypatch.setattr(h, 'check_host', lambda name, cfg, state: original(name, cfg, state, plan_runner([])))
    assert h.main([]) == 0
    out = capsys.readouterr().out
    digest = next(line for line in out.splitlines() if line.startswith('legs served per host: '))
    assert 'fiction 0/' in digest and 'managed_service_port_conflict' in digest
    record = json.loads((tmp_path / '.local/state/plangraph/host-ready/fiction.json').read_text())
    assert record['served_legs']['opencode-go-glm-5.3-flash'] == dict(
        served=False, reason='managed_service_port_conflict')
