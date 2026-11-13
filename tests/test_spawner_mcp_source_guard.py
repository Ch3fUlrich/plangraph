import asyncio
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from plangraph.spawner import git_guard, mcp, remote_ps


OLD, NEW = 'a' * 40, 'b' * 40
RESTART = f'restart required: {OLD[:12]} -> {NEW[:12]}, run /mcp reconnect'


def rpc(server, name, arguments=None):
    return asyncio.run(server.handle_rpc_request({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'plangraph_agent_' + name, 'arguments': arguments or {}}}))


@pytest.mark.parametrize('arguments', [{}, {'dry_run': False}, {'seats': True, 'role': 'review'}])
def test_changed_source_refuses_real_spawn_before_dispatch(tmp_path, monkeypatch, arguments):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.assert_called_once_with()
    identity.return_value = NEW
    spawn = AsyncMock(side_effect=AssertionError('spawn dispatched'))
    monkeypatch.setattr(server.service, 'spawn', spawn)
    monkeypatch.setattr(mcp.bench_entry, 'benchmark_options', Mock(side_effect=AssertionError('bench read')))
    reply = rpc(server, 'spawn', {'prompt': 'task', 'card': {'estimate_tokens': 1}, **arguments})
    assert reply['error']['code'] == -32000
    assert reply['error']['message'] == RESTART
    assert reply['error']['data']['type'] == 'SourceChanged'
    spawn.assert_not_called()
    assert not (tmp_path / 'runs').exists()


def test_health_reports_stale_and_reconnect_captures_new_identity(tmp_path):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    assert asyncio.run(server.call_tool('plangraph_agent_health', {}))['status'] == 'healthy'
    identity.return_value = NEW
    health = asyncio.run(server.call_tool('plangraph_agent_health', {}))
    assert health['status'] == 'stale' and health['error'] == RESTART
    reconnected = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    assert asyncio.run(reconnected.call_tool('plangraph_agent_health', {}))['status'] == 'healthy'


def test_stale_source_preserves_non_launch_tools(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value = NEW
    spawn = AsyncMock(return_value={'status': 'dry_run'})
    monkeypatch.setattr(server.service, 'spawn', spawn)
    monkeypatch.setattr(server.service, 'route', lambda *a, **kw: Mock(to_dict=lambda: {'plan': 'ok'}))
    monkeypatch.setattr(remote_ps, 'list_runs', lambda rows: rows)
    monkeypatch.setattr(server.runner, 'cancel', AsyncMock(return_value={'cancelled': True}))
    monkeypatch.setattr(server.service, 'cancel', AsyncMock(return_value={'cancelled': True}))
    monkeypatch.setattr(server.service, 'result', lambda run_id: {'run_id': run_id})
    cases = [('spawn', {'dry_run': True, 'prompt': 'task', 'card': {'estimate_tokens': 1}}),
             ('route', {'card': {'estimate_tokens': 1}}), ('ps', {}), ('status', {}),
             ('status', {'run_id': 'run-test'}), ('cancel', {'job_id': 'test'}),
             ('cancel', {'run_id': 'run-test'})]
    for name, arguments in cases:
        assert 'result' in rpc(server, name, arguments)
    assert spawn.await_args.kwargs['dry_run'] is True


def test_unchanged_source_allows_each_real_spawn(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    spawn = AsyncMock(return_value={'status': 'running'})
    monkeypatch.setattr(server.service, 'spawn', spawn)
    for _ in range(2):
        assert 'result' in rpc(server, 'spawn', {'prompt': 'task', 'card': {'estimate_tokens': 1}})
    assert spawn.await_count == 2 and identity.call_count == 3


def test_source_identity_uses_module_tree_not_cwd_or_git_redirect(tmp_path, monkeypatch):
    source = Path(git_guard.__file__).resolve().parent
    expected = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'unrelated.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path))
    assert git_guard.current_source_identity() == expected[:12]


@pytest.mark.parametrize('git_absent', [False, True])
def test_source_identity_without_git_hashes_python_tree(tmp_path, monkeypatch, git_absent):
    if git_absent:
        monkeypatch.setattr(git_guard.subprocess, 'run', Mock(side_effect=FileNotFoundError))
    source = tmp_path / 'spawner'
    source.mkdir()
    file = source / 'runner.py'
    file.write_text('old')
    identity = lambda: git_guard.current_source_identity(source)
    original = identity()
    assert len(original) == 12 and int(original, 16) >= 0
    (source / 'ignored.txt').write_text('not Python')
    assert identity() == original
    file.write_text('new')
    assert identity() != original
    file.write_text('old')
    nested = source / 'nested'
    nested.mkdir()
    (nested / 'added.py').write_text('extra')
    assert identity() != original
    (nested / 'added.py').unlink()
    assert identity() == original
    file.rename(source / 'renamed.py')
    assert identity() != original


def test_server_import_eagerly_loads_spawn_dependencies(tmp_path):
    modules = ['plangraph.spawner.' + name for name in ('runner', 'bench_entry', 'leg_probe', 'remote_ps')]
    modules.append('plangraph.planner.routing')
    script = ('import sys; import plangraph.spawner.mcp; '
              f'assert all(name in sys.modules for name in {modules!r}), '
              f'[name for name in {modules!r} if name not in sys.modules]')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    done = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
