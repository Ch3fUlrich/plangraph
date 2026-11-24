"""The spawner MCP serves new route code when the checkout head moves, without a process restart."""
import asyncio
import sys
from unittest.mock import Mock

import pytest

from plangraph.spawner import mcp, mcp_limits

OLD, NEW = 'a' * 12, 'b' * 12
SENTINEL = 'plangraph.planner.routing'


@pytest.fixture(autouse=True)
def restore_modules():
    """A reload rebinds module state process-wide: put every piece back after the test."""
    modules, namespace = dict(sys.modules), dict(mcp.__dict__)
    sources, names = dict(mcp_limits._SPAWN_SOURCES), mcp_limits.SPAWN_MODULES
    yield
    for name in [n for n in sys.modules if n not in modules]:
        del sys.modules[name]
    sys.modules.update(modules)
    mcp.__dict__.update(namespace)
    mcp_limits._SPAWN_SOURCES.clear()
    mcp_limits._SPAWN_SOURCES.update(sources)
    mcp_limits.SPAWN_MODULES = names


def call(server, name, arguments=None):
    return asyncio.run(server.call_tool('plangraph_agent_' + name, arguments or {}))


def moved_sources(monkeypatch):
    """The checkout after a route merge: the planner routing module carries a new attribute."""
    real = mcp_limits._spawn_modules

    def read(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        sources = {} if sources is None else sources
        names = real(root, entries, sources)
        path, text = sources[SENTINEL]
        sources[SENTINEL] = (path, text + '\nMERGED_ROUTE_CODE = True\n')
        return names
    monkeypatch.setattr(mcp_limits, '_spawn_modules', read)


def test_route_uses_the_new_module_after_the_head_moves(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    old_service = server.service
    routed = []
    monkeypatch.setattr(type(server.service), 'route', lambda self, *a, **k: routed.append(self) or
                        Mock(to_dict=lambda: {'ok': True}))
    assert call(server, 'route', {'card': {'estimate_tokens': 1}}) == {'ok': True}
    assert not hasattr(sys.modules[SENTINEL], 'MERGED_ROUTE_CODE')
    assert routed == [old_service]
    identity.return_value = NEW
    moved_sources(monkeypatch)
    assert call(server, 'route', {'card': {'estimate_tokens': 1}}) is not None
    assert sys.modules[SENTINEL].MERGED_ROUTE_CODE is True
    assert mcp.BenchRequired is sys.modules[SENTINEL].BenchRequired
    assert server.service is not old_service
    assert server._reload_error is None


def test_health_prints_the_served_sha_and_the_checkout_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'c' * 40)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=Mock(return_value=OLD))
    health = call(server, 'health')
    assert health['served_sha'] == 'c' * 40 and health['checkout_sha'] == 'c' * 40


def test_running_job_defers_the_reload(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    server.runner.jobs['j'] = Mock(status='running')
    identity.return_value = NEW
    moved_sources(monkeypatch)
    server.refresh_route_code()
    assert not hasattr(sys.modules[SENTINEL], 'MERGED_ROUTE_CODE')


def test_failed_reload_keeps_serving_the_previous_code(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    before, service = sys.modules[SENTINEL], server.service
    identity.return_value = NEW
    real = mcp_limits._spawn_modules

    def broken(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        names = real(root, entries, sources)
        path, _ = sources[SENTINEL]
        sources[SENTINEL] = (path, 'raise RuntimeError("bad merge")\n')
        return names
    monkeypatch.setattr(mcp_limits, '_spawn_modules', broken)
    server.refresh_route_code()
    assert sys.modules[SENTINEL] is before and server.service is service
    assert 'bad merge' in server._reload_error
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'd' * 40)
    assert 'bad merge' in call(server, 'health')['route_reload_error']
