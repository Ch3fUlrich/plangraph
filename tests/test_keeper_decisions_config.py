"""Install-time decisions mapping and a non-fatal startup warning."""
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_keeper_daemon import free_port, make_db_and_config, spawn, wait_health

from plangraph.keeper import config


def test_devstack_writer_configures_decisions_repo(tmp_path):
    path = tmp_path / 'keeper.json'
    checkout = tmp_path / 'decisions checkout'
    checkout.mkdir()
    config.write_devstack_config(path, 'postgresql://fixture', state=tmp_path,
                                port=8765, decisions_repo=checkout)
    raw = json.loads(path.read_text())
    assert raw['repos']['decisions'] == {'path': str(checkout.resolve()), 'ref': 'origin/main'}
    assert raw['backend'] == 'postgres'
    assert raw['dsn'] == 'postgresql://fixture'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load_config(path).repos['decisions']['ref'] == 'origin/main'


def test_devstack_writer_refuses_unconfigured_path_without_writing(tmp_path):
    path = tmp_path / 'keeper.json'
    with pytest.raises(config.ConfigError, match='PLANGRAPH_DECISIONS_REPO'):
        config.write_devstack_config(path, 'postgresql://fixture', state=tmp_path,
                                    port=8765, decisions_repo='')
    assert not path.exists()


def test_devstack_install_calls_the_config_writer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shell = (root / 'tools/devstack.sh').read_text()
    program = shell.split('cmd_keeper() {', 1)[1].split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    path = tmp_path / 'keeper.json'
    env = {**os.environ, 'PYTHONPATH': str(root / 'src'), 'DEVSTACK_STATE': str(tmp_path),
           'KEEPER_PORT': '8765', 'PLANGRAPH_DECISIONS_REPO': str(tmp_path / 'decisions')}
    subprocess.run([sys.executable, '-', str(path), 'postgresql://fixture'], input=program,
                   text=True, env=env, check=True, capture_output=True)
    assert json.loads(path.read_text())['repos']['decisions'] == {
        'path': str(tmp_path / 'decisions'), 'ref': 'origin/main'}


@pytest.mark.parametrize('configured', [False, True])
def test_startup_decisions_warning_is_nonfatal(tmp_path, configured):
    port = free_port()
    _, path = make_db_and_config(tmp_path, port)
    if configured:
        raw = json.loads(path.read_text())
        raw['repos'] = {'decisions': {'path': str(tmp_path / 'decisions'), 'ref': 'origin/main'}}
        path.write_text(json.dumps(raw))
    proc = spawn(path)
    try:
        assert wait_health(port)['status'] == 'ok'
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0
        lines = [line for line in stderr.decode().splitlines() if 'repos.decisions' in line]
        assert len(lines) == (0 if configured else 1)
        if lines:
            assert 'warning' in lines[0].lower() and 'missing' in lines[0].lower()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_existing_config_is_reused_without_decisions_environment(tmp_path):
    path = tmp_path / 'keeper.json'
    original = json.dumps({'db': 'existing.db', 'repos': {'demo': '/fixture/repo'},
                           'ci': {'demo': 'none'}, 'custom_setting': {'preserve': True}}) + '\n'
    path.write_text(original)
    path.chmod(0o600)
    config.write_devstack_config(path, 'postgresql://fixture', state=tmp_path,
                                port=8765, decisions_repo='')
    assert path.read_text() == original


def test_explicit_decisions_update_preserves_unrelated_config(tmp_path):
    path = tmp_path / 'keeper.json'
    original = {'repos': {'demo': '/fixture/repo'}, 'ci': {'demo': 'none'},
                'custom_setting': {'preserve': True}}
    path.write_text(json.dumps(original))
    path.chmod(0o600)
    config.write_devstack_config(path, 'postgresql://fixture', state=tmp_path,
                                port=8765, decisions_repo=tmp_path / 'decisions')
    updated = json.loads(path.read_text())
    assert updated['repos']['demo'] == original['repos']['demo']
    assert updated['ci'] == original['ci']
    assert updated['custom_setting'] == original['custom_setting']
    assert updated['repos']['decisions']['ref'] == 'origin/main'
