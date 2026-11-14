import json
import os
import subprocess
import shutil
import sys
from types import SimpleNamespace

import pytest
from plangraph.keeper import daemon


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    path = repo / 'src/plangraph/keeper/daemon.py'
    path.parent.mkdir(parents=True)
    path.write_text('VALUE = 1\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'loaded')
    loaded = git(repo, 'rev-parse', 'HEAD')
    return repo, None, loaded


def test_daemon_writes_loaded_sha_pid_and_start_time(checkout, tmp_path, monkeypatch):
    repo, _, loaded = checkout
    monkeypatch.setattr(daemon, 'CODE_TREE', repo, raising=False)
    monkeypatch.setattr(daemon, 'LOADED_IDENTITY', (loaded, 'main', False), raising=False)
    cfg = SimpleNamespace(db=tmp_path / 'keeper.db')
    path = daemon.write_loaded_state(cfg)
    row = json.loads(path.read_text())
    assert row['loaded_sha'] == loaded
    assert row['pid'] == os.getpid() and row['start_time'] > 0
    assert path.stat().st_mode & 0o777 == 0o600


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


@pytest.mark.parametrize('kind', ['dirty', 'detached', 'non-git', 'nested', 'skip-worktree', 'assume-unchanged'])
def test_loaded_state_describes_checkout_provenance(checkout, tmp_path, monkeypatch, kind):
    repo, _, loaded = checkout
    if kind == 'dirty':
        (repo / 'src/plangraph/keeper/daemon.py').write_text('DIRTY = True\n')
    elif kind == 'detached':
        git(repo, 'checkout', '--detach')
    elif kind == 'nested':
        repo = repo / 'install'
        repo.mkdir()
    elif kind in ('skip-worktree', 'assume-unchanged'):
        git(repo, 'update-index', '--' + kind, 'src/plangraph/keeper/daemon.py')
        (repo / 'src/plangraph/keeper/daemon.py').write_text('DIRTY = True\n')
    else:
        repo = tmp_path
    monkeypatch.setattr(daemon, 'CODE_TREE', repo)
    monkeypatch.setattr(daemon, 'LOADED_IDENTITY', (loaded, None if kind == 'detached' else 'main', False), raising=False)
    row = json.loads(daemon.write_loaded_state(SimpleNamespace(db=tmp_path / 'provenance.db')).read_text())
    dirty = kind in ('dirty', 'skip-worktree', 'assume-unchanged')
    assert row['loaded_branch'] == ('main' if dirty else None)
    assert row['dirty'] is (True if dirty else False if kind == 'detached' else None)
    assert row['loaded_sha'] == (loaded if kind == 'detached' else None)


@pytest.mark.parametrize('unlink_fails', [False, True])
def test_bounded_clean_exit_unlinks_state_before_exit_and_lock_release(tmp_path, monkeypatch, unlink_fails):
    import signal
    from test_keeper_daemon import make_db_and_config
    db, config_path = make_db_and_config(tmp_path, 0)
    cfg = daemon.config_mod.load_config(config_path)
    state = db.with_name(db.name + '.loaded.json')
    unlink = type(state).unlink
    def remove(path, *args, **kwargs):
        if path == state and unlink_fails:
            raise PermissionError('unlink denied')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(type(state), 'unlink', remove)
    real_thread = daemon.threading.Thread
    real_lock = daemon.kdb.lock_keeper
    exits, releases = [], []

    class StuckCleaner:
        def start(self): pass
        def join(self, timeout): pass
        def is_alive(self): return True

    def thread(*args, **kwargs):
        return StuckCleaner() if kwargs.get('name') == 'keeper-shutdown' else real_thread(*args, **kwargs)

    def lock(*args, **kwargs):
        held = real_lock(*args, **kwargs)
        release = held.release
        def release_checked():
            releases.append(state.exists())
            release()
        held.release = release_checked
        return held

    def exit_now(code):
        exits.append((code, state.exists()))
        raise SystemExit(code)

    monkeypatch.setattr(daemon.threading, 'Thread', thread)
    monkeypatch.setattr(daemon.kdb, 'lock_keeper', lock)
    monkeypatch.setattr(daemon.os, '_exit', exit_now)
    servers = []
    def ready(server):
        assert state.exists()
        servers.append(server)
        signal.raise_signal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit):
            daemon.run(cfg, ready=ready)
    finally:
        for server in servers:
            server.shutdown()
            server.stop_reaper()
            server.server_close()
    assert exits == [(0, unlink_fails)]
    assert releases == [unlink_fails]


def test_loaded_state_refuses_write_failure(tmp_path, monkeypatch, capsys):
    from plangraph.routing_support import private_io
    from test_keeper_daemon import make_db_and_config
    _, cfg = make_db_and_config(tmp_path, 0)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(private_io, 'atomic_write_private', fail)
    result = daemon.main(['--config', str(cfg)])
    assert result == 1
    assert capsys.readouterr().err == 'keeper: REFUSED loaded-state publication: disk full\n'


def test_head_moves_during_publication(checkout, tmp_path, monkeypatch):
    from plangraph.routing_support import private_io
    repo, _, loaded = checkout
    monkeypatch.setattr(daemon, 'CODE_TREE', repo)
    monkeypatch.setattr(daemon, 'LOADED_IDENTITY', (loaded, 'main', False), raising=False)
    write = private_io.atomic_write_private
    def moving(path, text):
        write(path, text)
        git(repo, 'commit', '--allow-empty', '-m', 'moved')
    monkeypatch.setattr(private_io, 'atomic_write_private', moving)
    row = json.loads(daemon.write_loaded_state(SimpleNamespace(db=tmp_path / 'moved.db')).read_text())
    assert row['loaded_sha'] is None and row['dirty'] is None


@pytest.mark.parametrize('target', ['views', 'backup_retention'])
def test_head_moves_before_keeper_import(checkout, tmp_path, target):
    repo, _, _ = checkout
    shutil.copytree(daemon.CODE_TREE / 'src/plangraph', repo / 'src/plangraph', dirs_exist_ok=True)
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'package')
    script = """
import builtins, json, subprocess, sys, threading
from types import SimpleNamespace
original = builtins.__import__
def importing(name, *args, **kwargs):
    if name == sys.argv[1] or (len(args) > 2 and sys.argv[1] in (args[2] or ())):
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'moved'], check=True)
    return original(name, *args, **kwargs)
builtins.__import__ = importing
from plangraph.keeper import daemon
path = daemon.write_loaded_state(SimpleNamespace(db='keeper.db'))
stop = threading.Event()
stop.set()
daemon._backup_prune_loop({}, stop)
print(path.read_text())
"""
    row = json.loads(subprocess.check_output([sys.executable, '-c', script, target], cwd=repo,
                     env={**os.environ, 'PYTHONPATH': str(repo / 'src')}, text=True))
    assert row['loaded_sha'] is None and row['dirty'] is None
