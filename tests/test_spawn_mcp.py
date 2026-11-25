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
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))  # local-only ps contract
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
                            model_or_combo='omniroute/cx-gpt-6.2-sol-medium', override_reason='audited RPC override')
        assert json.loads((tmp_path / 'spawn-overrides.jsonl').read_text())['reason'] == 'audited RPC override'
        assert (await call('ps'))['runs'][0]['run_id'] == record['run_id']
        assert (await call('result', run_id=record['run_id']))['status'] == 'dry_run'
        assert (await call('status', run_id=record['run_id']))['status'] == 'dry_run'
        assert (await call('cancel', run_id=record['run_id']))['status'] == 'cancelled'
    asyncio.run(run())


def test_refute_finding_6_mcp_review_scope_only_in_spawn():
    tools = {t['name']: t for t in TOOLS_METADATA}
    assert 'review_scope' in tools['plangraph_agent_spawn']['inputSchema']['properties']
    for name, tool in tools.items():
        if name != 'plangraph_agent_spawn':
            assert 'review_scope' not in tool['inputSchema'].get('properties', {}), (
                f"{name} should not have review_scope in its inputSchema"
            )

def test_native_rpc_wait_reports_only_settled_refusals(tmp_path, monkeypatch):
    record = dict(run_id='run-native', run_kind='native-codex', status='refused',
                  ended=None, exit_code=0, error='fixture refusal')
    calls = []

    async def run():
        server = MCPSpawnerServer(state=tmp_path)

        async def spawn(**kwargs):
            calls.append(('spawn', kwargs['wait_seconds']))
            return record.copy()

        async def wait(run_id, timeout):
            assert run_id == record['run_id']
            calls.append(('wait', timeout))
            raise TimeoutError('still settling')

        monkeypatch.setattr(server.service, 'spawn', spawn)
        monkeypatch.setattr(server.service, 'wait', wait)
        monkeypatch.setattr(server.service, 'result', lambda run_id: record.copy())
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'plangraph_agent_spawn', 'arguments': {
                'card': {'run_kind': 'native-codex', 'repo': str(tmp_path)},
                'prompt': 'fixture', 'wait_seconds': 0.1,
            }}}
        pending = await server.handle_rpc_request(request)
        assert 'error' not in pending, pending
        assert json.loads(pending['result']['content'][0]['text'])['ended'] is None
        assert calls[0] == ('spawn', 0)
        assert len(calls) == 2 and calls[1][0] == 'wait' and 0 <= calls[1][1] <= 0.1
        record['ended'] = 1
        settled = await server.handle_rpc_request(request)
        assert settled['error']['data']['run_id'] == record['run_id']
        assert settled['error']['data']['exit_code'] == 1

    asyncio.run(run())
