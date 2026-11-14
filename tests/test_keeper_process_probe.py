import json
import os
from types import SimpleNamespace

import pytest
from plangraph.fleet_sync.drift import keeper_main_pid


def test_keeper_unit_pid_uses_existing_service(monkeypatch):
    from plangraph.fleet_sync import drift

    def run(argv, **kwargs):
        assert argv == ['systemctl', '--user', 'show', 'plangraph-keeper.service', '--property=LoadState', '--property=MainPID']
        return SimpleNamespace(stdout='LoadState=loaded\nMainPID=123\n', returncode=0)

    monkeypatch.setattr(drift.subprocess, 'run', run)
    assert drift.keeper_main_pid('plangraph-keeper.service') == 123


@pytest.mark.parametrize('exe,argv,expected', [
    ('python3', 'python3 -m plangraph.keeper.daemon --config fixture.json', 123),
    ('python3', 'python3 -u /repo/src/plangraph/keeper/daemon.py --config fixture.json', 123),
    ('pypy3', 'pypy3 -X dev src/plangraph/keeper/daemon.py', 123),
    ('python3', 'python3 -m plangraph.keeper --config fixture.json', 123),
    ('vim', 'vim /repo/src/plangraph/keeper/daemon.py', 0),
    ('ps', 'ps -A -o pid= -o comm= -o args=', 0),
    ('python3', 'python3 editor.py /repo/src/plangraph/keeper/daemon.py', 0),
    ('python3', 'python3 -c "print(123)" /repo/src/plangraph/keeper/daemon.py', 0),
    ('python3', 'python3 -m plangraph.keeper.cli --help', 0),
    ('python3', 'python3 -c "unterminated', 0),
])
def test_keeper_probe_matches_interpreter_launch_target(monkeypatch, exe, argv, expected):
    from plangraph.fleet_sync import drift

    def run(command, **kwargs):
        assert command == ['ps', '-A', '-o', 'pid=', '-o', 'comm=', '-o', 'args=']
        return SimpleNamespace(stdout=f'123 {exe} {argv}\n', returncode=0)

    monkeypatch.setattr(drift.subprocess, 'run', run)
    assert drift.keeper_process_pid() == expected


def test_no_keeper_process_needs_no_pgrep(monkeypatch):
    from plangraph.fleet_sync import drift

    def run(command, **kwargs):
        if command[0] == 'pgrep':
            raise FileNotFoundError('fixture pgrep missing')
        assert command[0] == 'ps'
        return SimpleNamespace(stdout='123 vim vim daemon.py\n', returncode=0)

    monkeypatch.setattr(drift.subprocess, 'run', run)
    assert drift.keeper_process_pid() == 0


@pytest.mark.parametrize('exe,argv,expected', [
    ('pypy3.exe', r'pypy3.exe C:\repo\plangraph\keeper\daemon.py', 123),
    ('python.exe', 'python.exe -m plangraph.keeper.daemon', 123),
    ('notepad.exe', r'notepad.exe C:\repo\plangraph\keeper\daemon.py', 0),
    ('powershell.exe', 'powershell.exe probe plangraph.keeper.daemon', 0),
    ('System Idle Process', '', 0),
])
def test_windows_keeper_launch_targets(monkeypatch, exe, argv, expected):
    from plangraph.fleet_sync import drift

    def run(command, **kwargs):
        assert command[:4] == ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command']
        assert 'Get-CimInstance Win32_Process' in command[4]
        return SimpleNamespace(stdout=json.dumps([{'ProcessId': 0 if exe == 'System Idle Process' else 123, 'Name': exe, 'CommandLine': argv}]), returncode=0)

    monkeypatch.setattr(drift.subprocess, 'run', run)
    assert drift.keeper_process_pid(platform='nt') == expected


def test_keeper_console_entry_points_match_verified_launch_inventory():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    scripts = tomllib.loads((root / 'pyproject.toml').read_text()).get('project', {}).get('scripts', {})
    assert not {name: value for name, value in scripts.items() if 'keeper' in name or 'keeper' in value}, 'Extend keeper launch detection for new console entry points'



def test_empty_ps_args_does_not_hide_later_keeper(monkeypatch):
    from plangraph.fleet_sync import drift
    monkeypatch.setattr(drift.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='4242 python3\n123 python3 python3 -m plangraph.keeper.daemon\n', returncode=0))
    assert drift.keeper_process_pid() == 123


@pytest.mark.parametrize('live', [True, False])
def test_windows_state_liveness_never_signals(tmp_path, monkeypatch, live):
    from plangraph.fleet_sync import drift
    state = tmp_path / 'loaded.json'
    state.write_text(json.dumps({'pid': 123, 'loaded_sha': 'a' * 40}))
    signals = []
    monkeypatch.setattr(drift, 'os', SimpleNamespace(name='nt', getpid=os.getpid,
        kill=lambda *args: signals.append(args)))
    monkeypatch.setattr(drift.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=json.dumps([{'ProcessId': 123, 'Name': 'python.exe',
            'CommandLine': 'python.exe -m plangraph.keeper.daemon'}]) if live else '[]', returncode=0))
    result = drift.running_keeper(state)
    assert signals == []
    assert result == ('a' * 40 if live else None)


@pytest.mark.parametrize('failure,cause', [('missing', 'unavailable'), ('permission', 'unavailable'),
    ('timeout', 'timed out'), ('exit', 'failed'), ('malformed', 'unavailable')])
def test_process_query_failures_are_named(monkeypatch, failure, cause):
    import subprocess
    from plangraph.fleet_sync import drift

    def query(command, **kwargs):
        if failure == 'missing': raise FileNotFoundError()
        if failure == 'permission': raise PermissionError()
        if failure == 'timeout': raise subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(stdout='bad' if failure == 'malformed' else '',
                               returncode=2 if failure == 'exit' else 0)
    monkeypatch.setattr(drift.subprocess, 'run', query)
    with pytest.raises(drift.KeeperProbeError) as error:
        drift.keeper_process_pid()
    assert str(error.value) == 'keeper process query ' + cause
