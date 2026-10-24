"""CLI launch and host isolation contracts (no developer configuration needed)."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import cli, resolver


@pytest.mark.parametrize('installed', [True, False])
def test_supervisor_path_and_bounded_stderr(monkeypatch, tmp_path, capsys, installed):
    state = tmp_path / 'state'
    saved = []
    class Service:
        def __init__(self, **kw):
            self.state = state
        def route(self, *a, **kw):
            pass
    monkeypatch.setattr(cli, 'SpawnService', Service)
    monkeypatch.setenv('PYTHONPATH', '/existing/first')
    probes = []
    def probe(command, **kw):
        probes.append(command)
        return SimpleNamespace(returncode=0 if installed else 1)
    monkeypatch.setattr(subprocess, 'run', probe)
    def start(command, **kw):
        saved.append((command, kw))
        return SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(
            {'run_id': 'fake', 'status': 'running'} ) + '\n'))
    monkeypatch.setattr(cli, '_start_supervisor', start)
    assert cli.main(['--state', str(state), 'spawn', '--card', '{"task":"test"}', 'hello']) == 0
    command, kw = saved[0]
    assert probes == [[sys.executable, '-I', '-c', 'import plangraph']]
    root = str(Path(cli.__file__).resolve().parents[2])
    assert kw['env']['PYTHONPATH'] == root + os.pathsep + '/existing/first'
    log = Path(command[3])
    assert log.exists()
    assert log.name.startswith('supervisor-')
    # Exercise the actual child bootstrap with a fake supervisor, including raw fd writes.
    bootstrap = command[2]
    fake = """import os
service = supervisor.SpawnService(state=sys.argv[2])
service._save({'run_id': 'run-fake', 'status': 'running'})
original_replace = os.replace
def missing_source_once(source, destination):
    os.replace = original_replace
    os.unlink(source)
    raise FileNotFoundError(source)
os.replace = missing_source_once
for _ in range(400): os.write(2, b'x' * 8192)
os.write(2, b'rotation-recovered\\n')
"""
    bootstrap = bootstrap.replace('supervisor.main(["--supervise"])', 'exec(' + repr(fake) + ')')
    child_env = dict(kw['env'], PYTHONPATH=root)
    child = subprocess.Popen([sys.executable, '-c', bootstrap, str(log), str(state)], stderr=log.open('ab'), env=child_env)
    assert child.wait(timeout=10) == 0, log.read_text()[-2000:]
    assert log.stat().st_size > 0
    assert 'rotation-recovered' in log.read_text()
    assert sum(p.stat().st_size for p in state.glob('supervisor-*.stderr.log*')) <= 2 * 1024 * 1024
    assert 'supervisor_stderr_path' in bootstrap
    record = json.loads((state / 'runs/run-fake/record.json').read_text())
    assert record['supervisor_stderr_path'] == str(log)


def test_host_environment_is_pinned():
    assert Path(os.environ['AUTOOS_ROOT']).is_dir()
    assert not (Path(os.environ['AUTOOS_ROOT']) / 'opencode.jsonc').exists()
    import re
    assert not [name for name in os.environ if re.search(r'(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', name, re.I)]


@pytest.mark.real_probe
def test_real_probe_marker_bypasses_stub(real_served_models, monkeypatch):
    calls = []
    def run(command, **kw):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='provider/model\nnot a model\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert resolver.served_models('{}') == frozenset({'provider/model'})
    assert calls == [['opencode', 'models']]


def test_default_probe_is_stubbed(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('host probe escaped stub'))
    assert resolver.host_skip_reason({'harness': 'opencode', 'model': 'test/model'}) is None
