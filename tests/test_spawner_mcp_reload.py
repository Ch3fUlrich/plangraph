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
    for name, module in modules.items():  # package attributes bind each imported submodule
        parent = modules.get(name.rpartition('.')[0])
        if parent is not None and name.startswith('plangraph.'):
            setattr(parent, name.rpartition('.')[2], module)
    mcp.__dict__.update(namespace)
    mcp_limits._SPAWN_SOURCES.clear()
    mcp_limits._SPAWN_SOURCES.update(sources)
    mcp_limits.SPAWN_MODULES = names


def call(server, name, arguments=None):
    return asyncio.run(server.call_tool('plangraph_agent_' + name, arguments or {}))


def moved_sources(monkeypatch, **appended):
    """The checkout after a route merge: modules carry new source text (default: the routing module a sentinel)."""
    appended = appended or {SENTINEL: '\nMERGED_ROUTE_CODE = True\n'}
    real = mcp_limits._spawn_modules

    def read(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        sources = {} if sources is None else sources
        names = real(root, entries, sources)
        for module, text in appended.items():
            path, old = sources[module]
            sources[module] = (path, old + text)
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


def test_the_first_route_after_the_update_reads_its_benchmark_options_from_the_new_code(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value = NEW
    moved_sources(monkeypatch, **{'plangraph.spawner.bench_entry': (
        '\n_old = benchmark_options\nSEEN_BY_NEW_CODE = []\n'
        'def benchmark_options(*a, **k):\n    SEEN_BY_NEW_CODE.append(True)\n    return _old(*a, **k)\n')})
    call(server, 'route', {'card': {'estimate_tokens': 1}})
    assert sys.modules['plangraph.spawner.bench_entry'].SEEN_BY_NEW_CODE == [True]


def test_reload_keeps_job_history_and_run_tasks(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)

    async def scenario():
        finished = asyncio.ensure_future(asyncio.sleep(0))
        await finished
        server.service.tasks['run-done'] = finished
        server.service._running_records['j'] = {'run_id': 'run-done'}
        server.runner.jobs['j'] = Mock(status='completed')
        identity.return_value = NEW
        moved_sources(monkeypatch)
        server.refresh_route_code()
    asyncio.run(scenario())
    assert server._reload_error is None and sys.modules[SENTINEL].MERGED_ROUTE_CODE is True
    assert 'j' in server.runner.jobs
    assert 'run-done' in server.service.tasks and 'j' in server.service._running_records


def test_a_live_run_task_defers_the_reload_and_stays_cancellable(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    service = server.service

    async def scenario():
        live = asyncio.ensure_future(asyncio.sleep(3600))
        service.tasks['run-live'] = live
        identity.return_value = NEW
        moved_sources(monkeypatch)
        server.refresh_route_code()
        assert server.service is service and not live.done()
        live.cancel()
    asyncio.run(scenario())
    assert not hasattr(sys.modules[SENTINEL], 'MERGED_ROUTE_CODE')


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
    import importlib
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    before, service = sys.modules[SENTINEL], server.service
    bench = sys.modules['plangraph.spawner.bench_entry']
    identity.return_value = NEW
    # bench_entry imports cleanly as a candidate; a later module of the same package then fails
    moved_sources(monkeypatch, **{'plangraph.spawner.bench_entry': '\nREJECTED_CANDIDATE = True\n',
                                  'plangraph.spawner.service': '\nraise RuntimeError("bad merge")\n'})
    server.refresh_route_code()
    assert sys.modules[SENTINEL] is before and server.service is service
    assert 'bad merge' in server._reload_error
    from plangraph.spawner import bench_entry as from_form
    assert from_form is bench and not hasattr(from_form, 'REJECTED_CANDIDATE')
    assert importlib.import_module('plangraph.spawner.bench_entry') is bench
    assert sys.modules['plangraph.spawner'].bench_entry is bench
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'd' * 40)
    assert 'bad merge' in call(server, 'health')['route_reload_error']
