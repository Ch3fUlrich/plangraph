import asyncio
import json
import sqlite3

import pytest

from historical_legs import without_d4196

from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main
from plangraph.spawner.mcp import MCPSpawnerServer

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")

CARD = {'estimate_tokens': 1000}
WRITERS = tuple(l for l in without_d4196() if 'implement' in l.roles)
LEAD = next(l for l in WRITERS if l.id == LEAD_WRITER)


def test_historical_writers_keep_other_disables():
    assert next(l for l in without_d4196() if l.id == 'qodercli-qwen3.8-flash').disabled
    assert not any(l.disabled is None and 'D-3950' in (original.disabled or '')
                   for l in WRITERS for original in DEFAULT_LEGS if original.id == l.id)


def rows():
    return {l.model: {'aa_intelligence': 40 + i * 10,
                     'cost_per_task_usd': 0, 'pool_burn_weight': 0}
            for i, l in enumerate(WRITERS)}


def test_disabled_writer_never_routed():
    from dataclasses import replace
    disabled_writer = replace(LEAD, id='disabled-test-writer', disabled='synthetic disable')
    legs = (*DEFAULT_LEGS, disabled_writer)
    disabled = {l.id for l in legs if l.disabled and 'implement' in l.roles}
    assert disabled
    plan = plan_spawn(CARD, role='implement', benchmark=rows(), legs=legs)
    assert plan.legs and not disabled & {l.id for l in plan.legs}


@pytest.mark.parametrize('gemini_tier', ['free', 'subscription'])
def test_tier_first_and_swap(monkeypatch, gemini_tier):
    from mechanics_legs import install_fallthrough_legs
    writers = tuple(l for l in install_fallthrough_legs(monkeypatch) if 'implement' in l.roles)
    data = {l.model: {'aa_intelligence': 40 + i * 10,
                     'cost_per_task_usd': 0, 'pool_burn_weight': 0}
            for i, l in enumerate(writers)}
    # D-5212: both designated writers precede free legs, even at subscription cost.
    catalog = () if gemini_tier == 'free' else ({'id': 'agy/gemini-3.8-flash', 'tier': gemini_tier},)
    plan = plan_spawn(CARD, role='implement', benchmark=data, catalog_rows=catalog)
    expected = ('cx-gpt-6.1-sol-xhigh', 'agy-gemini-3.8-flash', 'opencode-big-pickle', 'openrouter-free',
                'opencode-glm-5.3-flash-free', 'opencode-deepseek-v4.1-flash-free',
                'omniroute-muse-spark', 'mimo-v2.6-pro', 'mimo-v2.6-flash',
                'agy-gemini-4-argon', 'omniroute-deepseek-flash', 'deepseek')
    assert tuple(l.id for l in plan.legs) == expected
    data[writers[0].model], data[writers[6].model] = data[writers[6].model], data[writers[0].model]
    swapped = plan_spawn(CARD, role='implement', benchmark=data, catalog_rows=catalog)
    assert tuple(l.id for l in swapped.legs) == (*expected[:2], expected[5], *expected[3:5], expected[2], *expected[6:])


def test_candidate_json_and_limit(monkeypatch):
    from mechanics_legs import install_fallthrough_legs
    writers = tuple(l for l in install_fallthrough_legs(monkeypatch) if 'implement' in l.roles)
    evidence = {'status': 429, 'body': 'quota exhausted'}
    def reader(model, **kwargs):
        return {'state': 'limited' if model == writers[6].model else 'ok',
                'evidence': evidence if model == writers[6].model else None}
    data = {l.model: {'aa_intelligence': 40, 'cost_per_task_usd': 0, 'pool_burn_weight': 0} for l in writers}
    del data[writers[0].model]
    plan = plan_spawn(CARD, role='implement', benchmark=data, leg_reader=reader, catalog_rows=())
    assert plan.first_leg == LEAD  # D-5206: designated effort-qualified writer.
    assert writers[6] not in plan.legs
    candidates = {c['leg']: c for c in plan.to_dict()['candidates']}
    assert candidates[writers[0].id]['u_core'] is None
    assert candidates[writers[0].id]['p_source'] == 'prior'
    assert isinstance(candidates[writers[5].id]['u_core'], float)
    assert candidates[writers[5].id]['p_source'] == 'measured'
    assert candidates[writers[6].id]['reason'] == 'limited'
    assert candidates[writers[6].id]['evidence'] == evidence
    assert candidates[writers[6].id]['tier'] == writers[6].tier


@pytest.mark.parametrize('probe', [test_tier_first_and_swap, test_candidate_json_and_limit])
def test_exact_tier_checks_ignore_ambient_catalog(monkeypatch, probe):
    def ambient_catalog():
        pytest.fail('exact-tier checks must not read the host catalog')

    monkeypatch.setattr('plangraph.routing_support.resolver.get_catalog_models', ambient_catalog)
    if probe is test_tier_first_and_swap:
        for tier in ('free', 'subscription'):
            probe(monkeypatch, tier)
    else:
        probe(monkeypatch)


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
        assert result['first_leg']['id'] == LEAD_WRITER
        assert result['p_source'] == 'benchmark'
        assert all(c['p_source'] == ('prior' if c.get('reason', '').startswith('disabled:') else 'measured')
                   and (c['u_core'] is None if c['p_source'] == 'prior' else isinstance(c['u_core'], float))
                   for c in result['candidates'])
