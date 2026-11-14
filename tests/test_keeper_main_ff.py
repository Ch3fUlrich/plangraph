"""Production fast-forward guard uses fixture repositories and state only."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.fleet_sync import advance
from plangraph.fleet_sync.__main__ import main
from plangraph.fleet_sync.drift import keeper_main_pid
from plangraph.keeper import daemon


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    from plangraph.fleet_sync import drift

    monkeypatch.setattr(drift, 'keeper_main_pid', lambda _unit: 0)
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    files = {'src/plangraph/__init__.py': '', 'src/plangraph/shared.py': 'def later():\n    from . import nested\n    from tools import keeper_helper\n    return nested.VALUE\n',
             'tools/__init__.py': '', 'tools/keeper_helper.py': 'VALUE = 1\n',
             'src/plangraph/nested.py': 'VALUE = 1\n',
             'src/plangraph/keeper/__init__.py': '',
             'src/plangraph/keeper/daemon.py': 'from plangraph import shared\n',
             'src/plangraph/spawner/unused.py': 'VALUE = 1\n'}
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'loaded')
    loaded = git(repo, 'rev-parse', 'HEAD')
    state = tmp_path / 'keeper-loaded.json'
    state.write_text(json.dumps({'loaded_sha': loaded, 'pid': os.getpid(), 'start_time': 1}))
    return repo, state, loaded


def target(repo, path, text):
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'target')
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/main', sha)
    git(repo, 'reset', '--hard', 'HEAD~1')
    return sha


@pytest.mark.parametrize('path,text,held', [
    ('src/plangraph/keeper/daemon.py', 'VALUE = 2\n', True),
    ('src/plangraph/shared.py', 'VALUE = 2\n', True),
    ('src/plangraph/nested.py', 'VALUE = 2\n', True),
    ('tools/keeper_helper.py', 'VALUE = 2\n', True),
    ('src/plangraph/keeper/unused_new.py', 'VALUE = 2\n', False),
    ('src/plangraph/spawner/unused.py', 'VALUE = 2\n', False),
])
def test_ff_checks_keeper_import_closure(checkout, path, text, held):
    repo, state, loaded = checkout
    sha = target(repo, path, text)
    code, line = advance.main_ff(repo, state, fetch=False)
    assert code == (3 if held else 0)
    assert git(repo, 'rev-parse', 'HEAD') == (loaded if held else sha)
    assert json.loads(state.read_text())['loaded_sha'] == loaded
    if held:
        assert line.startswith('HELD keeper code changed: ')
        assert path in line and f'loaded {loaded} target {sha}' in line


def test_no_keeper_state_allows_move(checkout):
    repo, state, _ = checkout
    sha = target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    state.unlink()
    assert advance.main_ff(repo, state, fetch=False)[0] == 0
    assert git(repo, 'rev-parse', 'HEAD') == sha


def test_unreadable_loaded_state_holds_instead_of_assuming_stopped(checkout):
    repo, state, loaded = checkout
    target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    state.write_text('{')
    assert advance.main_ff(repo, state, fetch=False)[0] == 3
    assert git(repo, 'rev-parse', 'HEAD') == loaded


def test_detached_and_dirty_are_refused(checkout):
    repo, state, loaded = checkout
    target(repo, 'src/plangraph/spawner/unused.py', 'VALUE = 2\n')
    git(repo, 'checkout', '--detach')
    assert advance.main_ff(repo, state, fetch=False) == (2, 'REFUSED branch HEAD, want main')
    git(repo, 'checkout', 'main')
    (repo / 'src/plangraph/shared.py').write_text('dirty\n')
    assert advance.main_ff(repo, state, fetch=False)[0] == 2
    assert git(repo, 'rev-parse', 'HEAD') == loaded


def test_default_command_fetches_and_advances_from_fixture_origin(checkout, tmp_path, capsys):
    repo, state, loaded = checkout
    origin = tmp_path / 'origin.git'
    git(tmp_path, 'init', '--bare', str(origin))
    git(repo, 'remote', 'add', 'origin', str(origin))
    git(repo, 'push', 'origin', 'main')
    sha = target(repo, 'src/plangraph/spawner/unused.py', 'VALUE = 2\n')
    git(repo, 'push', 'origin', sha + ':refs/heads/main')
    git(repo, 'update-ref', 'refs/remotes/origin/main', loaded)
    assert main(['main-ff', '--repo', str(repo), '--keeper-state', str(state)]) == 0
    assert git(repo, 'rev-parse', 'HEAD') == sha
    assert 'UPDATED main ' + sha in capsys.readouterr().out


def test_reused_pid_state_is_not_treated_as_running_keeper(checkout):
    repo, state, _ = checkout
    sha = target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    row = json.loads(state.read_text())
    row['pid_start'] = 'different-process-start'
    state.write_text(json.dumps(row))
    assert advance.main_ff(repo, state, fetch=False)[0] == 0
    assert git(repo, 'rev-parse', 'HEAD') == sha


def test_restart_state_is_rechecked_before_merge(checkout, monkeypatch):
    from plangraph.fleet_sync import drift

    repo, state, loaded = checkout
    target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    read = drift.running_keeper
    calls = []

    def restarting(path):
        calls.append(path)
        return None if len(calls) == 1 else read(path)

    monkeypatch.setattr(drift, 'running_keeper', restarting)
    monkeypatch.setattr(drift, 'keeper_main_pid', lambda _unit: 0, raising=False)
    assert advance.main_ff(repo, state, fetch=False)[0] == 3
    assert len(calls) == 2
    assert git(repo, 'rev-parse', 'HEAD') == loaded


@pytest.mark.parametrize('state_kind', ['missing', 'wrong-db'])
def test_live_unit_without_matching_state_holds(checkout, monkeypatch, state_kind):
    from plangraph.fleet_sync import drift

    repo, state, loaded = checkout
    target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    if state_kind == 'missing':
        state.unlink()
    else:
        state = state.with_name('wrong-db.loaded.json')
    monkeypatch.setattr(drift, 'keeper_main_pid', lambda _unit: os.getpid(), raising=False)
    code, line = advance.main_ff(repo, state, fetch=False)
    assert code == 3 and 'restart keeper' in line and 'state' in line
    assert git(repo, 'rev-parse', 'HEAD') == loaded


@pytest.mark.parametrize('path', ['src/plangraph/routing_support/catalog_schema.sql',
    'src/plangraph/planner/route_policy.json', 'configuration/opencode/providers.json'])
def test_existing_keeper_runtime_data_holds(checkout, path):
    repo, state, _ = checkout
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text('old data\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'loaded data')
    loaded = git(repo, 'rev-parse', 'HEAD')
    state.write_text(json.dumps({'loaded_sha': loaded, 'pid': os.getpid(), 'start_time': 1}))
    target(repo, path, 'new data\n')
    code, line = advance.main_ff(repo, state, fetch=False)
    assert code == 3 and path in line
    assert git(repo, 'rev-parse', 'HEAD') == loaded


@pytest.mark.parametrize('case,process_pid,held,cause', [
    ('missing-systemctl', 0, False, ''),
    ('not-found', 0, False, ''),
    ('bus', 0, True, 'bus unreachable'),
    ('timeout', 0, True, 'timed out'),
    ('oserror', 0, True, 'unit query unavailable'),
    ('bus', 123, True, 'live keeper'),
    ('not-found', 123, True, 'live keeper'),
    ('not-found', 'path', True, 'live keeper'),
    ('loaded-zero', 123, True, 'live keeper'),
    ('missing-systemctl', 123, True, 'live keeper'),
    ('system-pid', 0, True, 'live keeper'),
])
def test_absent_state_checks_unit_scopes_and_process(checkout, monkeypatch, case, process_pid, held, cause):
    from plangraph.fleet_sync import drift

    repo, state, loaded = checkout
    sha = target(repo, 'src/plangraph/spawner/unused.py', 'VALUE = 2\n')
    state.unlink()
    monkeypatch.setattr(drift, 'keeper_main_pid', keeper_main_pid)
    probe = drift.keeper_process_pid
    monkeypatch.setattr(drift, 'keeper_process_pid', lambda: probe() if process_pid == 'path' else process_pid)
    run = drift.subprocess.run
    scopes = []

    def query(argv, **kwargs):
        if argv[0] == 'ps':
            return SimpleNamespace(stdout='123 python3 python3 src/plangraph/keeper/daemon.py\n', returncode=0)
        if argv[0] != 'systemctl':
            return run(argv, **kwargs)
        scopes.append('--user' in argv)
        if case == 'missing-systemctl':
            raise FileNotFoundError('systemctl')
        if case == 'oserror':
            raise PermissionError('fixture unit unreadable')
        if case == 'timeout':
            raise subprocess.TimeoutExpired(argv, 5)
        if case == 'bus':
            return SimpleNamespace(stdout='', stderr='Failed to connect to bus: unavailable', returncode=1)
        if case == 'system-pid' and '--user' not in argv:
            return SimpleNamespace(stdout='LoadState=loaded\nMainPID=123\n', stderr='', returncode=0)
        load = 'loaded' if case == 'loaded-zero' else 'not-found'
        return SimpleNamespace(stdout=f'LoadState={load}\nMainPID=0\n', stderr='', returncode=0)

    monkeypatch.setattr(drift.subprocess, 'run', query)
    code, line = advance.main_ff(repo, state, fetch=False)
    assert code == (3 if held else 0)
    assert git(repo, 'rev-parse', 'HEAD') == (loaded if held else sha)
    assert cause in line
    if case in ('not-found', 'loaded-zero', 'system-pid'):
        assert False in scopes  # The system scope was checked as well.


@pytest.mark.parametrize('failure,cause', [('missing', 'unavailable'), ('oserror', 'unavailable'),
    ('timeout', 'timed out'), ('exit', 'failed'), ('malformed', 'unavailable')])
def test_process_probe_failures_hold_with_fixed_cause(checkout, monkeypatch, failure, cause):
    from plangraph.fleet_sync import drift

    repo, state, loaded = checkout
    target(repo, 'src/plangraph/spawner/unused.py', 'VALUE = 2\n')
    state.unlink()
    monkeypatch.setattr(drift, 'keeper_main_pid', keeper_main_pid)
    run = drift.subprocess.run

    def query(command, **kwargs):
        if command[0] == 'git':
            return run(command, **kwargs)
        if command[0] == 'systemctl':
            return SimpleNamespace(stdout='LoadState=not-found\nMainPID=0\n', returncode=0)
        if failure == 'missing':
            raise FileNotFoundError('fixture missing process tool')
        if failure == 'oserror':
            raise PermissionError('fixture unreadable process tool')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(stdout='bad fixture data' if failure == 'malformed' else '', returncode=2 if failure == 'exit' else 0)

    monkeypatch.setattr(drift.subprocess, 'run', query)
    code, line = advance.main_ff(repo, state, fetch=False)
    assert code == 3 and 'keeper process query ' + cause in line
    assert git(repo, 'rev-parse', 'HEAD') == loaded



@pytest.mark.parametrize('command', ['symbolic-ref', 'status', 'fetch', 'rev-parse', 'merge-base', 'merge'])
def test_git_failures_refresh_status_with_named_cause(checkout, monkeypatch, command):
    from plangraph.fleet_sync import drift
    repo, state, loaded = checkout
    target(repo, 'src/plangraph/spawner/unused.py', 'VALUE = 2\n')
    status = drift.main_ff_status_path(repo)
    status.write_text('HELD stale cause\n')
    run = drift.repo_git
    def fail(repo, *args, **kwargs):
        if args[0] == command and args[1:] != ('--absolute-git-dir',):
            raise subprocess.CalledProcessError(128, ['git', *args], stderr='private fixture stderr')
        if args[0] == 'fetch':
            return SimpleNamespace(returncode=0)
        return run(repo, *args, **kwargs)
    monkeypatch.setattr(drift, 'repo_git', fail)
    result = None
    try:
        result = advance.main_ff(repo, state)
    except subprocess.CalledProcessError:
        pass
    assert result is not None
    code, line = result
    assert code == 2 and 'REFUSED git ' + command + ' failed' == line
    assert status.read_text() == line + '\n'
    assert 'private' not in line
    assert git(repo, 'rev-parse', 'HEAD') == loaded
