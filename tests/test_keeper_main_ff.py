"""Production fast-forward guard uses fixture repositories and state only."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.fleet_sync import advance
from plangraph.fleet_sync.__main__ import main
from plangraph.keeper import daemon


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def checkout(tmp_path):
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


def test_held_is_visible_in_existing_drift_output(checkout, tmp_path, capsys):
    repo, state, _ = checkout
    target(repo, 'src/plangraph/keeper/daemon.py', 'VALUE = 2\n')
    _, line = advance.main_ff(repo, state, fetch=False)
    registry = tmp_path / 'hosts.json'
    registry.write_text(json.dumps({'hosts': [{'id': 'fixture', 'transport': 'local',
        'repos': {'plangraph': {'live_dir': str(repo)}}}]}))
    assert main(['drift', '--registry', str(registry)]) == 0
    assert line in capsys.readouterr().out


def test_daemon_writes_loaded_sha_pid_and_start_time(checkout, tmp_path, monkeypatch):
    repo, _, loaded = checkout
    monkeypatch.setattr(daemon, 'CODE_TREE', repo, raising=False)
    cfg = SimpleNamespace(db=tmp_path / 'keeper.db')
    path = daemon.write_loaded_state(cfg)
    row = json.loads(path.read_text())
    assert row['loaded_sha'] == loaded
    assert row['pid'] == os.getpid() and row['start_time'] > 0
    assert path.stat().st_mode & 0o777 == 0o600


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


def test_daemon_publishes_loaded_state_for_its_lifetime(tmp_path):
    import signal

    from test_keeper_daemon import free_port, make_db_and_config, spawn, wait_health

    port = free_port()
    db, cfg = make_db_and_config(tmp_path, port)
    path = db.with_name(db.name + '.loaded.json')
    process = spawn(cfg)
    try:
        assert wait_health(port)['status'] == 'ok'
        row = json.loads(path.read_text())
        assert row['pid'] == process.pid
        assert len(row['loaded_sha']) in (40, 64) and row['start_time'] > 0
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


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
