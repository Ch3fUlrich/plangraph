"""Final phase-review transport and persistence regressions."""
import asyncio
import json
import os
import sqlite3

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner, spawn_service


@pytest.mark.parametrize('store_kind', ['leg', 'catalog-default'])
@pytest.mark.parametrize('existing', [False, True])
def test_sqlite_private_sidecars(tmp_path, monkeypatch, store_kind, existing):
    from plangraph.spawner import catalog_db
    from plangraph.spawner.leg_state import LegStateStore
    path = tmp_path / 'private.db'
    if existing:
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
        path.chmod(0o644)
    old = os.umask(0o022)
    try:
        if store_kind == 'leg':
            db = LegStateStore(path)._connect()
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('INSERT INTO leg_events (leg_key) VALUES (?)', ('test/model',))
            db.commit()
        else:
            monkeypatch.setattr(catalog_db, 'DEFAULT_DB_PATH', path)
            store = catalog_db.CatalogDB(catalog_db.DEFAULT_DB_PATH)
            store.init_and_seed()
            db = store.conn
        for suffix in ('', '-wal', '-shm'):
            assert path.with_name(path.name + suffix).stat().st_mode & 0o777 == 0o600
        db.close()
    finally:
        os.umask(old)


class Process:
    pid = 123

    def __init__(self, output=(b'', b''), code=0, blocked=False):
        self.output, self.returncode, self.blocked = output, code, blocked

    async def communicate(self):
        if self.blocked:
            await asyncio.Event().wait()
        return self.output


@pytest.mark.parametrize('outcome', ['success', 'nonzero', 'provider-stop', 'cancel', 'spawn-failure'])
def test_nonce_removed_on_real_outcomes(monkeypatch, outcome):
    from plangraph.spawner import leg_state
    monkeypatch.setattr(leg_state, 'record_provider_response', lambda *a, **k: None)

    async def run():
        async def factory(*args, **kwargs):
            if outcome == 'spawn-failure':
                raise OSError('injected spawn failure')
            nonce = json.loads(kwargs['env']['OMNIROUTE_ENVELOPE_HEADERS'])['X-OmniRoute-Run-Nonce']
            envelope = json.dumps({'autoos_provider_response': dict(nonce=nonce, leg='test/model', status=429, body='quota exhausted', headers={})}).encode()
            return Process((b'', envelope if outcome == 'provider-stop' else b''), code=int(outcome == 'nonzero'), blocked=outcome == 'cancel')

        async def killer(pid):
            assert pid == 123

        runner = SpawnerRunner(process_factory=factory, tree_killer=killer)
        if outcome == 'spawn-failure':
            with pytest.raises(OSError, match='injected'):
                await runner.spawn('task', model_or_combo='mimo/mimo-v2.6-flash')
        else:
            job = await runner.spawn('task', model_or_combo='mimo/mimo-v2.6-flash')
            if outcome == 'cancel':
                await runner.cancel(job.job_id)
            await asyncio.gather(runner._tasks[job.job_id], return_exceptions=True)
            assert job.status == {'success': 'completed', 'nonzero': 'failed', 'provider-stop': 'completed', 'cancel': 'cancelled'}[outcome]
            if outcome == 'provider-stop':
                assert job.job_id in runner.provider_responses
        assert runner._nonces == {}
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['wrong', 'missing', 'other-run', 'stdout', 'matching'])
def test_nonce_transport_bypass(monkeypatch, kind):
    from plangraph.spawner import leg_state
    marks = []
    monkeypatch.setattr(leg_state, 'record_provider_response', lambda *a, **k: marks.append(a))

    async def run():
        runner = SpawnerRunner()
        runner.jobs['one'] = JobInfo('one', 'running', '', 'test/model')
        runner.jobs['two'] = JobInfo('two', 'running', '', 'test/model')
        runner._nonces.update(one='first', two='second')
        response = dict(leg='test/model', status=429, body='quota exhausted', headers={})
        if kind != 'missing':
            response['nonce'] = {'wrong': 'wrong', 'other-run': 'second'}.get(kind, 'first')
        envelope = json.dumps({'autoos_provider_response': response}).encode()
        await runner._monitor_process('one', Process((envelope, b'') if kind == 'stdout' else (b'', envelope)), 'test/model')
        assert len(marks) == int(kind == 'matching')
        assert runner._nonces == {'two': 'second'}
    asyncio.run(run())


def test_nonce_capacity_evicts_oldest_finished_first():
    async def run():
        runner = SpawnerRunner()
        for i in range(4096):
            key = str(i)
            runner._nonces[key] = key
            runner.jobs[key] = JobInfo(key, 'running' if i == 0 else 'completed', '', '', end_time=float(i))
        await runner.spawn('task', model_or_combo='mimo/mimo-v2.6-flash', dry_run=True)
        assert '0' in runner._nonces
        assert '1' not in runner._nonces
        assert '2' in runner._nonces
        assert len(runner._nonces) == 4095
    asyncio.run(run())


def test_mid_fallthrough_bench_refusal(tmp_path, monkeypatch):
    from plangraph.planner.routing import BenchRequired, plan_spawn
    from plangraph.spawner.service import SpawnService
    plan = plan_spawn({'estimate_tokens': 100}, role='implement')
    calls = []

    def route(self, *args, **kwargs):
        calls.append(kwargs)
        assert kwargs['require_bench'] is True
        if len(calls) > 1:
            raise BenchRequired('benchmark disappeared', [{'leg': plan.legs[1].id}])
        return plan

    monkeypatch.setattr(SpawnService, 'route', route)

    async def run():
        runner = SpawnerRunner()

        async def spawn(**kwargs):
            job = JobInfo('attempt', 'failed', '', kwargs['model_or_combo'])
            runner.provider_responses[job.job_id] = {'status': 429}
            return job

        monkeypatch.setattr(runner, 'spawn', spawn)
        service = spawn_service(runner=runner, state=tmp_path)
        record = await service.spawn(card={'estimate_tokens': 100}, prompt='task', require_bench=True, wait_seconds=None)
        assert record['status'] == 'refused'
        assert record['refusal'] == 'BenchRequired'
        assert record['exit_code'] == 3
        assert record['evidence'] == [{'leg': plan.legs[1].id}]
        assert len(record['attempts']) == 1
        assert record['attempts'][0]['skip_reason'] == 'provider_stop'
        assert record['ended'] is not None
        assert service.result(record['run_id']) == record
        from plangraph.spawner.cli import main
        assert main(['--state', str(tmp_path), 'result', record['run_id']]) == 3
        from plangraph.spawner.mcp import MCPSpawnerServer
        server = MCPSpawnerServer(state=tmp_path)
        calls.clear()
        server.service = service
        response = await server.handle_rpc_request({'id': 1, 'method': 'tools/call',
            'params': {'name': 'plangraph_agent_spawn', 'arguments': {'card': {'estimate_tokens': 100}, 'prompt': 'task', 'require_bench': True, 'wait_seconds': 1}}})
        assert response['error']['code'] == -32602
        assert response['error']['data']['type'] == 'BenchRequired'
        assert response['error']['data']['legs'] == [gap['leg'] for gap in record['evidence']]
        assert 'result' not in response
    asyncio.run(run())
