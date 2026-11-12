"""Registry diagnostics never expose config values or leak a temporary PATH."""
import asyncio
import json
import os

import pytest

from plangraph.spawner import doctor


@pytest.fixture(autouse=True)
def local_checks(monkeypatch):
    from types import SimpleNamespace
    from plangraph.planner import legs
    from plangraph.spawner import service
    async def ok(*args):
        return 'pass', 'ok'
    monkeypatch.setattr(legs, 'HARNESSES', ())
    monkeypatch.setattr(doctor, 'mcp', ok)
    monkeypatch.setattr(doctor, 'whoami', lambda: ('pass', 'ok'))
    monkeypatch.setattr(doctor, 'completion', lambda: ('pass', 'ok'))
    monkeypatch.setattr(doctor.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout='[]'))
    monkeypatch.setattr(service, 'SpawnService', lambda: SimpleNamespace(
        route=lambda *a, **k: SimpleNamespace(legs=[])))


@pytest.mark.parametrize('problem', ['identity', 'json', 'shape', 'env', 'path', 'unicode',
                                    'directory', 'hosts-map', 'aliases-string', 'host-shape'])
def test_unusable_registry_is_named(tmp_path, monkeypatch, problem):
    config = tmp_path / 'plangraph'
    config.mkdir()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', 'host-a')
    host = dict(id='host-a', env={'PATH': 'private-path-value'})
    registry = dict(self='host-a', hosts=[host])
    if problem == 'identity':
        host['id'] = 'host-b'
    elif problem == 'shape':
        registry['hosts'] = None
    elif problem == 'env':
        host['env'] = []
    elif problem == 'path':
        host['env']['PATH'] = 12
    elif problem == 'hosts-map':
        registry['hosts'] = {}
    elif problem == 'aliases-string':
        host['aliases'] = 'alias'
    elif problem == 'host-shape':
        registry['hosts'] = [12]
    path = config / 'hosts.json'
    if problem == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'\xff' if problem == 'unicode' else
                         b'{' if problem == 'json' else json.dumps(registry).encode())
    before = os.environ.get('PATH')
    rows = [r for r in asyncio.run(doctor.diagnose())['checks'] if r['name'] == 'host_path']
    assert rows == [dict(name='host_path', status='fail', detail=(
        'host identity mismatch: no matching id or alias' if problem == 'identity'
        else 'invalid host registry'))]
    assert (os.environ.get('PATH') == before) is True


@pytest.mark.parametrize('present', [False, True])
def test_path_restored_on_check_exception(tmp_path, monkeypatch, present):
    config = tmp_path / 'plangraph'
    config.mkdir()
    (config / 'hosts.json').write_text(json.dumps(dict(self='host-a', hosts=[
        dict(id='host-a', env={'PATH': 'candidate-path'})])))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_HOST_NAME', raising=False)
    monkeypatch.delenv('PATH', raising=False)
    if present:
        monkeypatch.setenv('PATH', 'inherited-path')
    def crash(name):
        assert os.environ['PATH'] == 'candidate-path'
        raise RuntimeError('check aborted')
    monkeypatch.setattr(doctor.shutil, 'which', crash)
    with pytest.raises(RuntimeError, match='check aborted'):
        asyncio.run(doctor.diagnose())
    assert (os.environ.get('PATH') == ('inherited-path' if present else None)) is True
