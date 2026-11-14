import json
import os
import subprocess
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


def test_daemon_writes_loaded_sha_pid_and_start_time(checkout, tmp_path, monkeypatch):
    repo, _, loaded = checkout
    monkeypatch.setattr(daemon, 'CODE_TREE', repo, raising=False)
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


@pytest.mark.parametrize('kind', ['dirty', 'detached', 'non-git'])
def test_loaded_state_describes_checkout_provenance(checkout, tmp_path, monkeypatch, kind):
    repo, _, loaded = checkout
    if kind == 'dirty':
        (repo / 'src/plangraph/keeper/daemon.py').write_text('DIRTY = True\n')
    elif kind == 'detached':
        git(repo, 'checkout', '--detach')
    else:
        repo = tmp_path
    monkeypatch.setattr(daemon, 'CODE_TREE', repo)
    row = json.loads(daemon.write_loaded_state(SimpleNamespace(db=tmp_path / 'provenance.db')).read_text())
    assert row['loaded_branch'] == ('main' if kind == 'dirty' else None)
    assert row['dirty'] is (True if kind == 'dirty' else False if kind == 'detached' else None)
    assert row['loaded_sha'] == (loaded if kind == 'detached' else None)



def test_bounded_clean_exit_unlinks_state_before_exit_and_lock_release(tmp_path, monkeypatch):
    import signal
    from test_keeper_daemon import make_db_and_config

    db, config_path = make_db_and_config(tmp_path, 0)
    cfg = daemon.config_mod.load_config(config_path)
    state = db.with_name(db.name + '.loaded.json')
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
    assert exits == [(0, False)]
    assert releases == [False]
