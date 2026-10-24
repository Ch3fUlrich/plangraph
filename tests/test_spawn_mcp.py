import asyncio
import json
from plangraph.spawner.runner import JobInfo
from plangraph.spawner.mcp import MCPSpawnerServer, TOOLS_METADATA


def test_missing_card_rpc_error(tmp_path):
    async def run():
        server = MCPSpawnerServer(state=tmp_path)
        response = await server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'plangraph_agent_spawn', 'arguments': {'prompt': 'task',
            'model_or_combo': 'mimo/mimo-v2.6-pro', 'override_reason': 'explicit override test', 'dry_run': True}}})
        assert response['error']['code'] == -32602
    asyncio.run(run())


def test_card_schema_and_roundtrip(tmp_path):
    tools = {t['name']: t for t in TOOLS_METADATA}
    assert 'card' in tools['plangraph_agent_spawn']['inputSchema']['required']
    assert all('plangraph_agent_' + verb in tools for verb in ('route', 'spawn', 'ps', 'result', 'status', 'cancel'))
    async def run():
        server = MCPSpawnerServer(state=tmp_path)
        record = await server.call_tool('plangraph_agent_spawn', {'card': {'estimate_tokens': 1000}, 'prompt': 'task', 'dry_run': True})
        result = await server.call_tool('plangraph_agent_result', {'run_id': record['run_id']})
        assert result['status'] == 'dry_run'
    asyncio.run(run())


def test_all_six_rpc_tools(tmp_path, monkeypatch):
    from plangraph.spawner import leg_state
    def forbidden(*a, **kw):
        raise AssertionError('MCP must not mutate leg state')
    monkeypatch.setattr(leg_state, 'record_provider_response', forbidden)
    monkeypatch.setattr(leg_state, 'record_probe_pass', forbidden)
    class Runner:
        jobs = {}
        _processes = {}
        _tasks = {}
        provider_responses = {}
        async def spawn(self, **kw):
            job = JobInfo('fake', 'dry_run', '', kw['model_or_combo'])
            self.jobs[job.job_id] = job
            return job
        async def cancel(self, job_id):
            self.jobs[job_id].status = 'cancelled'
    async def run():
        server = MCPSpawnerServer(Runner(), state=tmp_path)
        async def call(verb, **arguments):
            response = await server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'plangraph_agent_' + verb, 'arguments': arguments}})
            assert not response['result']['isError'], response
            return json.loads(response['result']['content'][0]['text'])
        listed = await server.handle_rpc_request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        schema = next(t for t in listed['result']['tools'] if t['name'] == 'plangraph_agent_spawn')
        assert 'card' in schema['inputSchema']['required']
        assert (await call('route', card={'estimate_tokens': 1000}))['first_leg']
        record = await call('spawn', card={'estimate_tokens': 1000}, prompt='task', dry_run=True,
                            model_or_combo='mimo/mimo-v2.6-pro', override_reason='audited RPC override')
        assert json.loads((tmp_path / 'spawn-overrides.jsonl').read_text())['reason'] == 'audited RPC override'
        assert (await call('ps'))['runs'][0]['run_id'] == record['run_id']
        assert (await call('result', run_id=record['run_id']))['status'] == 'dry_run'
        assert (await call('status', run_id=record['run_id']))['status'] == 'dry_run'
        assert (await call('cancel', run_id=record['run_id']))['status'] == 'cancelled'
    asyncio.run(run())
