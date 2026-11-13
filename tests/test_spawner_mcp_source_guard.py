import asyncio
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from plangraph.spawner import mcp, mcp_limits, remote_ps


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


def _git(repo, *args):
    env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(repo.parent)}
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                   check=True, capture_output=True, env=env)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    root = tmp_path / 'repo' / 'plangraph'
    (root / 'planner').mkdir(parents=True)
    (root / 'planner' / 'routing.py').write_text('x = 1\n')
    _git(root.parent, 'init', '-q')
    _git(root.parent, 'add', '.')
    _git(root.parent, 'commit', '-q', '-m', 'one')
    return root


def _guarded(tmp_path, tree):
    return mcp.MCPSpawnerServer(state=tmp_path / 'state',
                                source_identity=lambda: mcp_limits.current_source_identity(tree))


def _assert_refuses_real_but_not_dry(server, monkeypatch):
    monkeypatch.setattr(server.service, 'spawn', AsyncMock(return_value={'status': 'running'}))
    reply = rpc(server, 'spawn', {'prompt': 'task', 'card': {'estimate_tokens': 1}})
    assert reply['error']['data']['type'] == 'SourceChanged'
    assert reply['error']['message'].startswith('restart required: ')
    assert 'result' in rpc(server, 'spawn', {'dry_run': True, 'prompt': 'task', 'card': {'estimate_tokens': 1}})
    health = asyncio.run(server.call_tool('plangraph_agent_health', {}))
    assert health['status'] == 'stale' and health['error'].startswith('restart required: ')


def test_moved_git_head_refuses_real_run(tmp_path, monkeypatch, tree):
    server = _guarded(tmp_path, tree)
    _git(tree.parent, 'commit', '-q', '--allow-empty', '-m', 'two')
    _assert_refuses_real_but_not_dry(server, monkeypatch)


def test_uncommitted_edit_outside_spawner_refuses_real_run(tmp_path, monkeypatch, tree):
    server = _guarded(tmp_path, tree)
    (tree / 'planner' / 'routing.py').write_text('x = 22222\n')
    _assert_refuses_real_but_not_dry(server, monkeypatch)


def test_non_git_fallback_covers_files_outside_spawner(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    root = tmp_path / 'plangraph'
    (root / 'planner').mkdir(parents=True)
    (root / 'spawner').mkdir()
    (root / 'planner' / 'routing.py').write_text('x = 1\n')
    (root / 'spawner' / 'runner.py').write_text('y = 1\n')
    server = _guarded(tmp_path, root)
    (root / 'planner' / 'routing.py').write_text('x = 22222\n')
    _assert_refuses_real_but_not_dry(server, monkeypatch)


def test_unreadable_file_and_missing_tree_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    (tmp_path / 'a.py').write_text('x')
    (tmp_path / 'gone.py').symlink_to(tmp_path / 'missing.py')
    first, second = (mcp_limits.current_source_identity(tmp_path) for _ in range(2))
    assert len(first) == 12 and first != second
    assert len(mcp_limits.current_source_identity(tmp_path / 'absent')) == 12


@pytest.mark.parametrize('name', ['plangraph_session_launch', 'plangraph_session_relaunch'])
def test_session_starts_are_guarded_other_tools_are_not(tmp_path, name):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value = NEW
    with pytest.raises(mcp_limits.SourceChanged):
        server._source_guard.check(name, {})
    for open_tool in ('plangraph_session_stop', 'plangraph_session_list', 'plangraph_session_mark_ready',
                      'plangraph_agent_cancel'):
        server._source_guard.check(open_tool, {})


def test_server_import_eagerly_loads_spawn_dependencies(tmp_path):
    script = r"""
import asyncio, sys
from pathlib import Path
from plangraph.spawner.mcp import MCPSpawnerServer
from plangraph.spawner import base_fresh, seat_spawn
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
server = MCPSpawnerServer(state=Path.cwd() / 'state')
base_fresh._check = lambda *a, **k: dict(ok=True, origin_sha='b'*40, base_sha='b'*40, behind=0, reason=None)
seat_spawn.resources = lambda *a: (0, 1000000)
leg = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-xhigh')
server.service._route = lambda *a, **k: SpawnPlan((leg,), (), 'test', (), ())
before = set(sys.modules)
result = asyncio.run(server.service.spawn(card={'estimate_tokens':100}, prompt='Implement a tested change',
    dry_run=True, wait_seconds=None))
assert result['status'] == 'dry_run', result
loaded = {name for name in sys.modules if name.startswith('plangraph.')}
assert not loaded.difference(before), sorted(loaded.difference(before))
"""
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'PLANGRAPH_OVERFLOW_HOSTS': str(tmp_path / 'absent-overflow.json')}
    done = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
