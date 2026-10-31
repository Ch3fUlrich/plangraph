import asyncio
import json
import sqlite3

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main
from plangraph.spawner.mcp import MCPSpawnerServer

CARD = {'estimate_tokens': 1000}
WRITERS = tuple(l for l in DEFAULT_LEGS if 'implement' in l.roles)


def rows():
    return {l.model: {'aa_intelligence': 40 + i * 10,
                     'cost_per_task_usd': 0, 'pool_burn_weight': 0}
            for i, l in enumerate(WRITERS)}


def test_tier_first_and_swap():
    data = rows()
    plan = plan_spawn(CARD, role='implement', benchmark=data)
    assert plan.first_leg == WRITERS[4]
    # D-3259: expectations are named here, never computed from the leg tables
    # the router consumes.  The implement plan is the operator writer order --
    # console credit legs lead WRITER_ORDER or are skipped (the credit gate
    # skips one here) -- and the writer outside WRITER_ORDER, the paid deepseek
    # leg, closes the plan.
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    assert [leg.id for leg in plan.legs] == [
        'agy-gemini-3.8-flash', 'mimo-v2.6-pro', 'openrouter-free',
        'opencode-deepseek-v4.1-flash-free', 'opencode-glm-5.3-flash-free',
        'mimo-v2.6-flash', 'cx-gpt-6.1-sol', 'deepseek']
    assert plan.legs[-1].id == 'deepseek'
    assert plan.legs[-1].tier == 'paid'
    assert plan.legs[-1] == by_id['deepseek']
    assert [(s['leg'], s['reason']) for s in plan.skipped] == [('console-sonnet-5.5', 'credit_expired')]
    data[WRITERS[4].model], data[WRITERS[6].model] = data[WRITERS[6].model], data[WRITERS[4].model]
    swapped = plan_spawn(CARD, role='implement', benchmark=data)
    assert swapped.first_leg == WRITERS[4]
    assert [leg.id for leg in swapped.legs] == [leg.id for leg in plan.legs]


def test_candidate_json_and_limit():
    evidence = {'status': 429, 'body': 'quota exhausted'}
    def reader(model, **kwargs):
        return {'state': 'limited' if model == WRITERS[6].model else 'ok',
                'evidence': evidence if model == WRITERS[6].model else None}
    data = rows()
    del data[WRITERS[0].model]
    plan = plan_spawn(CARD, role='implement', benchmark=data, leg_reader=reader)
    assert plan.first_leg == WRITERS[4]
    assert WRITERS[6] not in plan.legs
    candidates = {c['leg']: c for c in plan.to_dict()['candidates']}
    assert candidates[WRITERS[0].id]['u_core'] is None
    assert candidates[WRITERS[0].id]['p_source'] == 'prior'
    assert isinstance(candidates[WRITERS[5].id]['u_core'], float)
    assert candidates[WRITERS[5].id]['p_source'] == 'measured'
    assert candidates[WRITERS[6].id]['reason'] == 'limited'
    assert candidates[WRITERS[6].id]['evidence'] == evidence
    assert candidates[WRITERS[6].id]['tier'] == WRITERS[6].tier


def test_default_database_cli_mcp(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'bench.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE catalog_benchmarks (model_id TEXT, aa_intelligence REAL, cost_per_task_usd REAL, pool_burn_weight REAL)')
        conn.executemany('INSERT INTO catalog_benchmarks VALUES (?, ?, ?, ?)',
                         [(model, r['aa_intelligence'], 0, 0) for model, r in rows().items()])
    monkeypatch.setenv('PLANGRAPH_CATALOG_DB', str(path))
    assert main(['--state', str(tmp_path), 'route', '--card', json.dumps(CARD), '--json']) == 0
    cli = json.loads(capsys.readouterr().out)
    mcp = asyncio.run(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_route', {'card': CARD}))
    for result in (cli, mcp):
        assert result['first_leg']['id'] == WRITERS[4].id
        assert result['p_source'] == 'operator-order'
        assert all(c['p_source'] == 'measured' and isinstance(c['u_core'], float)
                   for c in result['candidates'])
