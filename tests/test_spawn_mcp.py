import asyncio
from plangraph.spawner.mcp import MCPSpawnerServer, TOOLS_METADATA


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
