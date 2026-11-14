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
    missing_first, missing_second = (mcp_limits.current_source_identity(tmp_path / 'absent') for _ in range(2))
    assert missing_first != missing_second
    guard = mcp_limits.SourceGuard(lambda: mcp_limits.current_source_identity(tmp_path / 'absent'))
    assert guard.restart_required() is not None


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


def test_server_pins_lazy_spawn_dependencies_without_optional_posix_imports(tmp_path):
    script = r"""
import importlib, sys
from pathlib import Path
from plangraph.spawner.mcp import MCPSpawnerServer
from plangraph.spawner import mcp_limits
sys.modules['fcntl'] = None
server = MCPSpawnerServer(state=Path.cwd() / 'state', source_identity=lambda: 'old')
names = ('planner.route_policy', 'review.family', 'keeper.coverage', 'keeper.evidence_repo',
         'review.gateway', 'spawner.overflow', 'fleet_sync.genconfig', 'spawner.cli',
         'routing_support.served_fresh', 'keeper.evidence_decisions')
assert all('plangraph.' + name in mcp_limits.SPAWN_MODULES for name in names)
# Package source/bytecode loading must never read the updated tree.
original_get_code = importlib.machinery.SourceFileLoader.get_code
def guarded_get_code(loader, name):
    assert not name.startswith('plangraph.'), 'read live package source after startup'
    return original_get_code(loader, name)
importlib.machinery.SourceFileLoader.get_code = guarded_get_code
for name in names:
    importlib.import_module('plangraph.' + name)
assert 'plangraph.keeper.db' not in sys.modules
"""
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    done = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_new_lazy_imports_and_transitive_sources_are_derived_and_pinned(tmp_path, monkeypatch):
    import importlib
    root = tmp_path / 'plangraph'
    package = root / 'guard_fixture'
    package.mkdir(parents=True)
    (root / '__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    entry = package / 'entry.py'
    entry.write_text('def run():\n    from . import child\n    return child.value\n')
    child = package / 'child.py'
    child.write_text('from plangraph.guard_fixture.leaf import value\n')
    leaf = package / 'leaf.py'
    leaf.write_text('value = "old"\n')
    sources = {}
    modules = mcp_limits._spawn_modules(root, ('plangraph.guard_fixture.entry',), sources)
    assert set(modules) == {'plangraph', 'plangraph.guard_fixture',
                            'plangraph.guard_fixture.entry', 'plangraph.guard_fixture.child',
                            'plangraph.guard_fixture.leaf'}
    monkeypatch.setattr(mcp_limits, '_SPAWN_SOURCES', sources)
    entry.write_text('raise AssertionError("new entry executed")\n')
    child.write_text('raise AssertionError("new child executed")\n')
    leaf.unlink()
    try:
        module = importlib.import_module('plangraph.guard_fixture.entry')
        assert 'plangraph.guard_fixture.child' not in sys.modules
        assert module.run() == 'old'
    finally:
        for name in modules:
            if name.startswith('plangraph.guard_fixture'):
                sys.modules.pop(name, None)


@pytest.mark.parametrize('name', ['plangraph_agent_spawn', 'plangraph_session_launch',
                                  'plangraph_session_relaunch'])
@pytest.mark.parametrize('arguments', [None, [], 'invalid', 1])
@pytest.mark.parametrize('stale', [False, True])
def test_non_object_launch_arguments_are_rpc_refusals(tmp_path, name, arguments, stale):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    if stale:
        identity.return_value = NEW
    reply = asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}))
    assert reply['error']['code'] == (-32000 if stale else -32602)
    assert reply['error']['data']['type'] == ('SourceChanged' if stale else 'ParameterRefused')


def test_stale_session_relaunch_cannot_use_unadvertised_dry_run(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value = NEW
    sessions = Mock()
    monkeypatch.setattr(server, '_sessions', lambda: sessions)
    reply = asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'plangraph_session_relaunch', 'arguments': {'name': 'review', 'dry_run': True}}}))
    assert reply['error']['data']['type'] == 'SourceChanged'
    assert reply['error']['message'] == RESTART
    sessions.relaunch.assert_not_called()
    sessions.audit_refusal.assert_not_called()


@pytest.mark.parametrize('git_tree', [False, True])
def test_package_asset_edit_invalidates_source(tmp_path, monkeypatch, git_tree):
    root = tmp_path / 'package'
    root.mkdir()
    asset = root / 'template.toml'
    asset.write_text('value = 1\n')
    if git_tree:
        _git(root, 'init', '-q')
        _git(root, 'add', '.')
        _git(root, 'commit', '-q', '-m', 'asset')
    server = _guarded(tmp_path, root)
    asset.write_text('value = 222222\n')
    _assert_refuses_real_but_not_dry(server, monkeypatch)
