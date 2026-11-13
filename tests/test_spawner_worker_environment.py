"""Import-path and parent admission regressions for fresh worker handoff."""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plangraph.spawner import git_guard
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('installed', [True, False])
@pytest.mark.parametrize('checkout', [True, False])
@pytest.mark.parametrize('inherited', [None, '', 'before', os.pathsep + 'before' + os.pathsep])
def test_local_import_fallback_respects_install_probe(tmp_path, monkeypatch, installed, checkout, inherited):
    import plangraph
    root = tmp_path / ('src' if checkout else 'site-packages')
    root.mkdir()
    if checkout: (tmp_path / 'pyproject.toml').touch()
    monkeypatch.setattr(plangraph, '__file__', str(root / 'plangraph/__init__.py'))
    probe = Mock(return_value=SimpleNamespace(returncode=0 if installed else 1))
    monkeypatch.setattr(git_guard.subprocess, 'run', probe)
    if inherited is None: monkeypatch.delenv('PYTHONPATH', raising=False)
    else: monkeypatch.setenv('PYTHONPATH', inherited)
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    assert hasattr(service, 'code_tree') and not service.fresh_tree
    env, context = git_guard.worker_environment(service)
    with context as tree: assert tree is None
    probe.assert_called_once()
    assert probe.call_args.args[0][1:] == ['-I', '-c', 'import plangraph']
    parts = ['before'] if inherited and 'before' in inherited else []
    expected = [str(root), *parts] if checkout else parts + ([] if installed else [str(root)])
    assert env.get('PYTHONPATH') == (os.pathsep.join(expected) if expected else None)
    assert '' not in env.get('PYTHONPATH', 'absent').split(os.pathsep)


def test_fresh_import_selection_skips_local_probe(tmp_path, monkeypatch):
    chosen = []
    @contextmanager
    def tree(state):
        chosen.append(state); yield {'tree_path': str(tmp_path)}
    monkeypatch.setattr(git_guard.subprocess, 'run', Mock(side_effect=AssertionError('local probe')))
    service = SimpleNamespace(fresh_tree=True, state=tmp_path, code_tree=tree)
    _, context = git_guard.worker_environment(service)
    with context as selected: assert selected['tree_path'] == str(tmp_path)
    assert chosen == [tmp_path]


@pytest.mark.parametrize('outcome', ['queued', 'deferred', 'refused', 'accepted'])
def test_parent_admission_precedes_fresh_handoff(tmp_path, monkeypatch, outcome):
    from plangraph.routing_errors import SpawnRefused
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    service.fresh_tree = True
    events = []
    def admit(options, **kwargs):
        events.append('admit')
        if outcome == 'refused': raise SpawnRefused('parent refusal')
        if outcome in ('queued', 'deferred'): return {'run_id': 'run-admitted', 'status': outcome}
    async def handoff(owner, options):
        events.append('handoff'); return {'run_id': 'run-child', 'status': 'running'}
    monkeypatch.setattr(service, 'admit_spawn', admit)
    monkeypatch.setattr(git_guard, 'fresh_spawn', handoff)
    if outcome == 'refused':
        with pytest.raises(SpawnRefused, match='parent refusal'):
            asyncio.run(service.spawn(prompt='Implement the requested change.'))
        assert service.ps()['runs'][0]['refusal'] == 'SpawnRefused'
    else:
        result = asyncio.run(service.spawn(prompt='Implement the requested change.'))
        assert result['status'] == ('running' if outcome == 'accepted' else outcome)
    assert events == (['admit', 'handoff'] if outcome == 'accepted' else ['admit'])


@pytest.mark.parametrize('envelope', [{'seats': None}, {'seats': []},
    {'seats': [], 'writers': [{'run_id': 'run-child', 'status': 'completed', 'exit_code': 0}]}])
@pytest.mark.parametrize('installed', [False, True])
def test_injected_cli_keeps_local_imports_and_handles_empty_seats(tmp_path, monkeypatch, capsys, envelope, installed):
    import io
    import json
    import plangraph
    from plangraph.spawner import cli
    root = tmp_path / ('site-packages' if installed else 'src'); root.mkdir()
    if not installed: (tmp_path / 'pyproject.toml').touch()
    monkeypatch.setattr(plangraph, '__file__', str(root / 'plangraph/__init__.py'))
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.setattr(git_guard.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=0)))
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)
    monkeypatch.setattr(service, 'route', Mock())
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: service)
    def no_tree(*args): raise AssertionError('injected CLI selected Git tree')
    monkeypatch.setattr(service, 'code_tree', no_tree)
    class Input(io.StringIO):
        def close(self): pass
    class Supervisor:
        def __init__(self, argv, **kwargs):
            self.stdin, self.stdout = Input(), self
            assert kwargs['cwd'] is None
            expected = None if installed else str(root)
            assert kwargs['env'].get('PYTHONPATH') == expected
            assert '' not in kwargs['env'].get('PYTHONPATH', 'absent').split(os.pathsep)
        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            assert payload['state'] == str(tmp_path) and '_tree' not in payload
            return json.dumps(dict(run_id='run-local', status='completed', exit_code=0, **envelope))
        def close(self): pass
    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)
    assert cli.main(['--state', str(tmp_path), 'spawn', '--card', '{"task":"test"}',
        'Implement the requested change.']) == 0
    assert json.loads(capsys.readouterr().out)['seats'] == envelope['seats']
    assert service.injected_runner and not service.fresh_tree


@pytest.mark.parametrize('envelope', [{'seats': []}, {'seats': None}, {'writers': []},
    {'seats': [], 'writers': []}])
def test_cli_empty_envelope_without_status_fails(tmp_path, monkeypatch, capsys, envelope):
    from plangraph.spawner import cli
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    async def spawn(**kwargs): return envelope
    monkeypatch.setattr(service, 'spawn', spawn)
    monkeypatch.setattr(service, 'route', Mock())
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: service)
    assert cli.main(['spawn', '--dry-run', '--card', '{"task":"test"}',
        'Implement the requested change.']) == 1
    import json
    assert json.loads(capsys.readouterr().out) == envelope


def test_supervisor_consumes_parent_admission_once(tmp_path, monkeypatch, capsys):
    import io
    import json
    from plangraph.spawner import cli
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    calls = []
    def admit(options, **kwargs):
        calls.append('admit'); return {'run_id': 'run-queued', 'status': 'queued'}
    async def spawn(prompt='', wait_seconds=0, _placement=None):
        calls.append('launch')
        assert _placement == {'host': 'local', 'reason': 'local_fallback_no_remote'}
        return {'run_id': 'run-child', 'status': 'completed', 'exit_code': 0}
    monkeypatch.setattr(service, 'admit_spawn', admit)
    monkeypatch.setattr(service, '_spawn', spawn)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: service)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(dict(state=str(tmp_path),
        prompt='Implement the requested change.', _admitted=True,
        _placement={'host': 'local', 'reason': 'local_fallback_no_remote'}))))
    class Output(io.StringIO):
        def close(self): pass
    output = Output(); monkeypatch.setattr('sys.stdout', output)
    assert cli.main(['--supervise']) == 0
    assert json.loads(output.getvalue())['run_id'] == 'run-child'
    assert calls == ['launch']
    assert asyncio.run(service.spawn(prompt='Implement another change.'))['status'] == 'queued'
    assert calls == ['launch', 'admit']


@pytest.mark.parametrize('error', [OSError('probe unavailable'), git_guard.subprocess.TimeoutExpired('probe', 10)])
def test_probe_failure_still_supplies_package_root(tmp_path, monkeypatch, error):
    import plangraph
    root = tmp_path / 'site-packages'; root.mkdir()
    monkeypatch.setattr(plangraph, '__file__', str(root / 'plangraph/__init__.py'))
    monkeypatch.setenv('PYTHONPATH', '')
    monkeypatch.setattr(git_guard.subprocess, 'run', Mock(side_effect=error))
    env, _ = git_guard.worker_environment(SimpleNamespace(fresh_tree=False))
    assert env['PYTHONPATH'] == str(root)
