"""The spawner MCP serves new route code when the checkout head moves, without a process restart."""
import asyncio
import json
import os
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


def test_a_live_run_task_never_delays_the_reload_and_stays_cancellable(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    old = server.service

    async def scenario():
        live = asyncio.ensure_future(asyncio.sleep(3600))
        old.tasks['run-live'] = live
        server.runner.jobs['j'] = Mock(status='running')
        identity.return_value = NEW
        moved_sources(monkeypatch)
        server.refresh_route_code()
        assert server.service is not old and server.service.tasks['run-live'] is live
        assert sys.modules[SENTINEL].MERGED_ROUTE_CODE is True
        assert not live.done()
        server.service.tasks['run-live'].cancel()   # the same task object: the new service owns it
        await asyncio.sleep(0)
        assert live.cancelled()
    asyncio.run(scenario())


def test_package_bindings_are_cleared_so_candidates_import_the_new_sibling(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value = NEW
    moved_sources(monkeypatch, **{'plangraph.spawner.env_redact': '\nMERGED_SETTING = True\n'})
    server.refresh_route_code()
    fresh = sys.modules['plangraph.spawner.env_redact']
    assert fresh.MERGED_SETTING is True
    assert sys.modules['plangraph.spawner.runner'].env_redact is fresh   # `from . import env_redact`
    assert sys.modules['plangraph.spawner'].env_redact is fresh


def test_service_construction_failure_restores_the_previous_generation(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'a' * 40)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    before, service, bench = sys.modules[SENTINEL], server.service, sys.modules['plangraph.spawner.bench_entry']
    identity.return_value = NEW
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'b' * 40)
    moved_sources(monkeypatch, **{
        'plangraph.spawner.bench_entry': '\nREJECTED_CANDIDATE = True\n',
        'plangraph.spawner.service': '\ndef _boom(self, *a, **k):\n    raise RuntimeError("ctor")\n'
                                     'SpawnService.__init__ = _boom\n'})
    server.refresh_route_code()
    assert 'ctor' in server._reload_error
    assert sys.modules[SENTINEL] is before and sys.modules['plangraph.spawner.bench_entry'] is bench
    assert not hasattr(bench, 'REJECTED_CANDIDATE') and mcp.bench_entry is bench
    assert server.service is service and server._served_head == 'a' * 40
    assert call(server, 'health')['served_sha'] == 'a' * 40


def test_the_legacy_status_rpc_survives_a_reload_of_retained_jobs(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    server.runner.jobs['j'] = mcp.JobInfo(job_id='j', status='completed', task_prompt='p', model_or_combo='m')
    first = call(server, 'status', {'job_id': 'j'})
    identity.return_value = NEW
    moved_sources(monkeypatch)
    server.refresh_route_code()
    assert sys.modules[SENTINEL].MERGED_ROUTE_CODE is True
    assert call(server, 'status', {'job_id': 'j'}) == first
    assert call(server, 'status')['jobs'][0]['job_id'] == 'j'


def test_an_idle_session_follows_the_head_and_the_deploy_report_prints_every_served_sha(tmp_path, monkeypatch, capsys):
    heads = {'now': 'a' * 40}
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: heads['now'])
    monkeypatch.setattr(mcp_limits, 'checkout_head', lambda source=None: heads['now'])
    monkeypatch.setattr(mcp, 'running_mcp_pids', lambda: {os.getpid()})
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    assert mcp.served_main(tmp_path) == 0
    assert 'current' in capsys.readouterr().out
    heads['now'] = 'b' * 40
    identity.return_value = NEW
    moved_sources(monkeypatch)
    assert mcp.served_main(tmp_path) == 1          # the session still serves the old head
    assert 'STALE' in capsys.readouterr().out

    async def idle():
        follower = asyncio.ensure_future(mcp.keep_current(server, 0.01))
        await asyncio.sleep(0.5)                    # no request arrives
        follower.cancel()
    asyncio.run(idle())
    assert sys.modules[SENTINEL].MERGED_ROUTE_CODE is True
    assert mcp.served_main(tmp_path) == 0
    assert ('pid=%d' % os.getpid()) in capsys.readouterr().out


def test_the_report_never_signals_the_sessions_it_inspects(tmp_path, monkeypatch):
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        mcp_limits.publish_served(tmp_path, 'a' * 40, OLD)
        row = tmp_path / mcp_limits.SERVED_DIR / f'{os.getpid()}.json'
        other = tmp_path / mcp_limits.SERVED_DIR / f'{child.pid}.json'
        from plangraph.mcp_supervise.procutil import process_start_time
        data = json.loads(row.read_text())
        other.write_text(json.dumps({**data, 'pid': child.pid, 'start': process_start_time(child.pid)}))
        from plangraph.mcp_supervise import procutil
        asked = []
        real_alive = procutil.pid_alive
        monkeypatch.setattr(procutil, 'pid_alive', lambda pid: asked.append(pid) or real_alive(pid))
        assert 'os.kill' not in open(mcp_limits.__file__, encoding='utf-8').read()   # TerminateProcess on Windows
        rows = mcp_limits.served_report(tmp_path, checkout='a' * 40, running={child.pid, os.getpid()})
        assert {r['pid'] for r in rows} == {child.pid, os.getpid()}
        assert child.poll() is None and sorted(asked) == sorted([child.pid, os.getpid()])
    finally:
        child.kill()
        child.wait()
    monkeypatch.undo()
    assert [r['pid'] for r in mcp_limits.served_report(tmp_path, checkout='a' * 40, running={os.getpid()})] == [os.getpid()]
    assert not other.exists()                       # a dead session's file is removed


def test_unregistered_sessions_fail_the_deploy_report(tmp_path, monkeypatch, capsys):
    mcp_limits.publish_served(tmp_path, 'a' * 40, OLD)
    rows = mcp_limits.served_report(tmp_path, checkout='a' * 40, running={os.getpid(), 4242424})
    old = [r for r in rows if r['pid'] == 4242424][0]
    assert old['served_sha'] is None and old['registered'] is False and old['current'] is False
    monkeypatch.setattr(mcp, 'running_mcp_pids', lambda: {os.getpid(), 4242424})
    monkeypatch.setattr(mcp_limits, 'checkout_head', lambda source=None: 'a' * 40)
    assert mcp.served_main(tmp_path) == 1
    assert 'unregistered' in capsys.readouterr().out
    monkeypatch.setattr(mcp, 'running_mcp_pids', lambda: None)       # inventory unavailable
    assert mcp.served_main(tmp_path) == 1
    monkeypatch.setattr(mcp, 'running_mcp_pids', lambda: set())      # no session at all
    assert mcp.served_main(tmp_path, report=[]) == 1


def test_a_failed_candidate_service_leaves_the_runner_hooks_on_the_old_generation(tmp_path, monkeypatch):
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    hooks = (server.runner.on_sample, server.runner.state)
    identity.return_value = NEW
    moved_sources(monkeypatch, **{'plangraph.spawner.seat_spawn':
                                  '\ndef live_runner(runner):\n    raise RuntimeError("live")\n'})
    server.refresh_route_code()
    assert 'live' in server._reload_error
    assert (server.runner.on_sample, server.runner.state) == hooks


def test_the_served_sha_is_the_head_whose_source_was_loaded(tmp_path, monkeypatch):
    heads = {'now': 'a' * 40}
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: heads['now'])
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    real, reads = mcp_limits._spawn_modules, []

    def read(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        names = real(root, entries, sources)
        reads.append(heads['now'])
        if len(reads) == 1:                          # the checkout moves right after snapshot B was read
            heads['now'], identity.return_value = 'c' * 40, 'c' * 12
        return names
    heads['now'], identity.return_value = 'b' * 40, NEW
    monkeypatch.setattr(mcp_limits, '_spawn_modules', read)
    server.refresh_route_code()
    assert reads == ['b' * 40, 'c' * 40]             # B was loaded, the change was noticed, C was loaded
    assert server._served_head == 'c' * 40 and server._reload_error is None


def test_a_checkout_that_never_settles_installs_nothing_and_keeps_the_old_sha(tmp_path, monkeypatch):
    heads = {'n': 0}
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'a' * 40)
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    service, before = server.service, sys.modules[SENTINEL]
    real = mcp_limits._spawn_modules

    def read(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        names = real(root, entries, sources)
        heads['n'] += 1
        identity.return_value = f'{heads["n"]:012d}'   # the checkout moves after every read
        return names
    identity.return_value = NEW
    monkeypatch.setattr(mcp_limits, '_spawn_modules', read)
    server.refresh_route_code()
    assert heads['n'] == 3 and 'changed during reload' in server._reload_error
    assert server.service is service and sys.modules[SENTINEL] is before and server._served_head == 'a' * 40
    assert json.loads((tmp_path / mcp_limits.SERVED_DIR / f'{os.getpid()}.json').read_text())['served_sha'] == 'a' * 40


def test_a_checkout_that_returns_to_the_served_head_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: 'a' * 40)
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    service, before = server.service, sys.modules[SENTINEL]
    real = mcp_limits._spawn_modules

    def read(root=None, entries=('plangraph.spawner.mcp',), sources=None):
        names = real(root, entries, sources)
        identity.return_value = OLD                    # B was read, then the checkout went back to A
        return names
    identity.return_value = NEW
    monkeypatch.setattr(mcp_limits, '_spawn_modules', read)
    server.refresh_route_code()
    assert server.service is service and sys.modules[SENTINEL] is before
    assert server._reload_error is None and server._served_head == 'a' * 40


def test_the_inventory_matches_the_server_invocation_only(monkeypatch):
    listing = '\n'.join([
        '10 python3 -m plangraph.spawner.mcp',
        '11 grep plangraph.spawner.mcp',
        '12 vim notes on plangraph.spawner.mcp',
        '13 python3 -m plangraph.spawner.mcp --served-sha',
        '14 /usr/bin/python3 -u -m plangraph.spawner.mcp',
        '15 less /tmp/-m plangraph.spawner.mcp.log',
        '16 python3 -c import time -m plangraph.spawner.mcp',
        '17 python3 tool.py -m plangraph.spawner.mcp',
        '18 python3 -X dev -m plangraph.spawner.mcp'])
    monkeypatch.setattr(mcp_limits.subprocess, 'run', lambda *a, **k: Mock(stdout=listing))
    assert mcp_limits.running_mcp_pids() == {10, 14, 18}


def test_a_registration_of_a_reused_pid_is_not_a_session_and_a_late_server_is_kept(tmp_path):
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        directory = tmp_path / mcp_limits.SERVED_DIR
        directory.mkdir()
        stale = directory / f'{child.pid}.json'
        stale.write_text(json.dumps({'pid': child.pid, 'start': 'another-process', 'served_sha': 'a' * 40,
                                     'route_reload_error': None}))
        assert mcp_limits.served_report(tmp_path, checkout='a' * 40, running=set()) == []
        assert not stale.exists()                       # recycled pid: removed, never counted
        from plangraph.mcp_supervise.procutil import process_start_time
        stale.write_text(json.dumps({'pid': child.pid, 'start': process_start_time(child.pid),
                                     'served_sha': 'a' * 40, 'route_reload_error': None}))
        # a live registered process the census did not see (it started after the census) is kept and listed
        listed = mcp_limits.served_report(tmp_path, checkout='a' * 40, running={7})
        assert [r['pid'] for r in listed if r['registered']] == [child.pid] and stale.exists()
    finally:
        child.kill()
        child.wait()


def test_a_real_python_c_process_with_the_module_as_script_arguments_is_not_a_server():
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '-m', 'plangraph.spawner.mcp'])
    try:
        assert child.pid not in mcp_limits.running_mcp_pids()
    finally:
        child.kill()
        child.wait()


def test_a_transient_git_failure_does_not_leave_the_served_sha_empty(tmp_path, monkeypatch):
    head = {'now': 'a' * 40}
    monkeypatch.setattr(mcp, 'checkout_head', lambda source=None: head['now'])
    identity = Mock(return_value=OLD)
    server = mcp.MCPSpawnerServer(state=tmp_path, source_identity=identity)
    identity.return_value, head['now'] = NEW, ''          # git HEAD lookup fails while the snapshot installs
    moved_sources(monkeypatch)
    server.refresh_route_code()
    assert sys.modules[SENTINEL].MERGED_ROUTE_CODE is True and server._served_head == ''
    head['now'] = 'b' * 40                                # the next refresh binds the sha without a reload
    server.refresh_route_code()
    assert server._served_head == 'b' * 40
    published = json.loads((tmp_path / mcp_limits.SERVED_DIR / f'{os.getpid()}.json').read_text())
    assert published['served_sha'] == 'b' * 40
