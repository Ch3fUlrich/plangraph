use std::path::Path;
use std::process::Command;

#[test]
fn benchmark_rows_keep_effort_harness_provenance_and_missing_values() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let state = tempfile::tempdir().unwrap();
    let output = Command::new("python3")
        .current_dir(&repo)
        .env("PYTHONPATH", repo.join("src"))
        .env("PLANGRAPH_CATALOG_DB", state.path().join("catalog.sqlite"))
        .args([
            "-c",
            r#"
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from plangraph.planner.benchmarks import benchmark_for_leg
from plangraph.planner.legs import DEFAULT_LEGS, leg_active, spawnable_legs
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.spawner.bench_entry import benchmark_options
from plangraph.spawner.mcp import MCPSpawnerServer

with CatalogDB(os.environ['PLANGRAPH_CATALOG_DB']) as db:
    db.init_and_seed()
    model = 'mimo/mimo-v2.6-pro'
    required_priors = {
        ('omniroute/cx-gpt-6.2-sol-medium', 'medium', 'opencode'),
        ('omniroute/cx-gpt-6.2-sol-high', 'high', 'opencode'),
        ('omniroute/cx-gpt-6.2-sol-xhigh', 'xhigh', 'opencode'),
        ('codex/gpt-6.2-sol-medium', 'medium', 'codex'),
        ('claude/claude-fable-5-2', 'low', 'claude'),
        ('claude/claude-fable-5-2', 'medium', 'claude'),
        ('claude/claude-fable-5-2', 'high', 'claude'),
        ('agy/gemini-4-argon-medium', 'medium', 'agy'),
        ('mimo/mimo-v2.6-pro', '', 'opencode'),
        ('mimo/mimo-v2.6-flash', '', 'opencode'),
        ('agy/gemini-4-hydrogen-high', 'high', 'agy'),
        ('agy/gemini-4-hydrogen-medium', 'medium', 'agy'),
        ('agy/gemini-4-neon-medium', 'medium', 'agy'),
        ('agy/claude-opus-5-5-low', 'low', 'agy'),
        ('agy/claude-opus-5-5-medium', 'medium', 'agy'),
        ('agy/claude-opus-5-5-high', 'high', 'agy'),
    }
    seeded = {(r['model_id'], r['effort'], r['harness']): r for r in db.conn.execute(
        'SELECT * FROM catalog_benchmark_rows WHERE legacy = 0')}
    assert required_priors <= seeded.keys(), required_priors - seeded.keys()
    assert all(r['source_date'] and r['source_record'] and r['source_url'] for r in seeded.values())
    assert all(seeded[key]['imputed'] for key in required_priors)
    def record(effort, harness, origin, score, cost=None, at='2000-01-01'):
        db.conn.execute('''INSERT INTO catalog_benchmark_measurements
            (model_id, effort, harness, origin, score, cost_per_task_usd, measured_at, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (model, effort, harness, origin, score, cost, at, 'https://example.invalid/bench'))
    record('high', 'mimo', 'aa-index', 90)
    record('high', 'mimo', 'deepswe', 80)
    record('high', 'mimo', 'own', 0.0, 0.0)
    record('low', 'mimo', 'cursorbench', 13)
    record('low', 'mimo', 'frontierswe', 66)
    record('low', 'mimo', 'aa-index', 90)
    record('low', 'opencode', 'frontierswe', 20)
    for origin in ('aider', 'livebench', 'swe-bench-verified'):
        try:
            record('high', 'mimo', origin, 100)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(origin)
    db.conn.commit()
    rows = {(row['effort'], row['harness']): row for row in db.get_model(model)['benchmark_rows']}
    own = rows['high', 'mimo']
    assert own['origin'] == 'own' and not own['imputed']
    assert own['score'] == own['cost_per_task_usd'] == 0.0 and own['stale']
    assert own['n_runs'] is None and own['tokens_per_task'] is None and own['score_se'] is None
    assert rows['low', 'mimo']['imputed'] and rows['low', 'mimo']['cost_per_task_usd'] is None
    assert rows['low', 'mimo']['origin'] == 'cursorbench'
    assert rows['low', 'opencode']['score'] == 20
    data = benchmark_options()['benchmark']
    assert {(r['effort'], r['harness']): r for r in data[model]['benchmark_rows']} == rows
    for leg in spawnable_legs():
        key = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
        cells = data.get(key, {}).get('benchmark_rows', ())
        if leg_active(leg, now=float('inf')) and any(
                cell['score'] is not None and cell['harness'] in ('', leg.harness)
                and (cell['effort'] == (leg.effort or '') or (cell['legacy'] and leg.effort is None))
                for cell in cells):
            assert benchmark_for_leg(data, leg) is not None, leg.id
    muse = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-go-muse-spark')
    assert benchmark_for_leg(data, muse)['effort'] == 'xhigh'
    assert benchmark_for_leg(data, replace(muse, effort='low')) is None
    codex = next(leg for leg in spawnable_legs() if leg.id == 'gpt-6.2-sol-medium')
    assert benchmark_for_leg(data, codex)['score'] == 53
    luna_legs = [leg for leg in spawnable_legs() if leg.id.startswith('cx-gpt-6-luna-')]
    assert {leg.effort for leg in luna_legs} == {'low', 'medium', 'high', 'xhigh', 'max'}
    assert all(benchmark_for_leg(data, leg)['imputed'] for leg in luna_legs)
    base = next(leg for leg in DEFAULT_LEGS if leg.id == 'mimo-v2.6-pro')
    assert benchmark_for_leg(data, replace(base, effort='high')) is None
    leg = replace(base, effort='low')
    def leg_status(*args, **kwargs):
        return {'state': 'ok', 'evidence': None}
    plan = plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('gpt',),
                      legs=(leg,), benchmark=data, strict=True, leg_reader=leg_status,
                      catalog_rows=db.list_models())
    assert not plan.gaps and plan.candidates[0]['u_core'] is not None

    # Call the public catalog query with a deterministic live-state reader.
    server = SimpleNamespace(_source_guard=SimpleNamespace(check=lambda *a: None))
    with patch('plangraph.spawner.mcp.get_catalog_models', db.list_models), \
            patch('plangraph.spawner.mcp.leg_status', leg_status):
        queried = asyncio.run(MCPSpawnerServer.call_tool(server, 'plangraph_agent_query_catalog', {}))
    assert next(row for row in queried['models'] if row['id'] == model)['benchmark_rows'] == list(rows.values())
    assert {(r['model_id'], r['effort'], r['harness']) for r in queried['benchmark_rows']} >= required_priors
    db.init_and_seed()
    assert any(row['origin'] == 'own' for row in db.get_model(model)['benchmark_rows'])
    export = db.export_json()
with CatalogDB() as restored:
    restored.init_and_seed()
    restored.import_json(export)
    assert any(row['origin'] == 'own' for row in restored.get_model(model)['benchmark_rows'])
with CatalogDB() as legacy:
    legacy.conn.executescript(Path('src/plangraph/spawner/catalog_schema.sql').read_text(encoding='utf-8'))
    assert legacy.export_json()['benchmarks'] == []
"#,
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
