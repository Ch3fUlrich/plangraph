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
        .env(
            "PLANGRAPH_BENCH_PENDING",
            repo.join("configuration/benchmark_data_pending.json"),
        )
        .args([
            "-c",
            r#"
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from plangraph.planner.benchmarks import BenchRequired, benchmark_for_leg
from plangraph.planner.legs import DEFAULT_LEGS, leg_active, spawnable_legs
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.spawner.bench_entry import benchmark_options
from plangraph.spawner.mcp import MCPSpawnerServer

with CatalogDB(os.environ['PLANGRAPH_CATALOG_DB']) as db:
    db.init_and_seed()
    model = 'mimo/mimo-v2.6-pro'
    def record(effort, harness, origin, score, cost=None, at='2000-01-01', model_id=model):
        db.conn.execute('''INSERT INTO catalog_benchmark_measurements
            (model_id, effort, harness, origin, score, cost_per_task_usd, measured_at, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (model_id, effort, harness, origin, score, cost, at, 'https://example.invalid/bench'))
    for family in ('gpt-6.2-sol', 'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'mimo-v2.6'):
        model_id = 'fixture/' + family
        query = 'SELECT imputed FROM catalog_benchmark_rows WHERE model_id=?'
        record('medium', 'fixture', 'aa-index', 75, model_id=model_id)
        assert db.conn.execute(query, (model_id,)).fetchone()[0] == 1, family
        record('medium', 'fixture', 'own', 50, model_id=model_id)
        assert db.conn.execute(query, (model_id,)).fetchone()[0] == 0, family
    recent, stale = db.conn.execute("SELECT datetime('now', '-44 days'), datetime('now', '-46 days')").fetchone()
    record('high', 'mimo', 'aa-index', 90)
    record('high', 'mimo', 'deepswe', 80)
    record('high', 'mimo', 'own', 0.0, 0.0, at=stale)
    record('low', 'mimo', 'cursorbench', 13)
    record('low', 'mimo', 'frontierswe', 66)
    record('low', 'mimo', 'aa-index', 90)
    record('low', 'opencode', 'deepswe', 20, at=recent)
    record('low', 'opencode', 'cursorbench', 100, at=recent)
    record('max', 'fixture', 'aa-index', 90)
    record('max', 'fixture', 'frontierswe', 60)
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
    assert {'model_id', 'effort', 'harness', 'score', 'score_se', 'n_runs', 'steps',
            'cost_per_task_usd', 'tokens_per_task', 'imputed', 'effort_channel',
            'index_version', 'source_url', 'measured_at', 'origin'} <= own.keys()
    assert own['origin'] == 'own' and not own['imputed']
    assert own['score'] == own['cost_per_task_usd'] == 0.0 and own['stale']
    assert own['n_runs'] is None and own['tokens_per_task'] is None and own['score_se'] is None
    assert rows['low', 'mimo']['imputed'] and rows['low', 'mimo']['cost_per_task_usd'] is None
    assert rows['low', 'mimo']['origin'] == 'cursorbench'
    assert rows['low', 'opencode']['score'] == 20 and not rows['low', 'opencode']['stale']
    assert rows['max', 'fixture']['origin'] == 'frontierswe'
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
    base = next(leg for leg in DEFAULT_LEGS if leg.id == 'mimo-v2.6-pro')
    missing = replace(base, effort='high')
    assert benchmark_for_leg(data, missing) is None
    leg = replace(base, effort='low')
    def leg_status(*args, **kwargs):
        return {'state': 'ok', 'evidence': None}
    plan = plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('gpt',),
                      legs=(leg,), benchmark=data, strict=True, leg_reader=leg_status,
                      catalog_rows=db.list_models())
    assert not plan.gaps and plan.candidates[0]['u_core'] is not None
    try:
        plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('gpt',),
                   legs=(missing,), benchmark=data, strict=True, leg_reader=leg_status,
                   catalog_rows=db.list_models())
    except BenchRequired as error:
        assert error.gaps == [{'leg': missing.id, 'model': missing.model, 'reason': 'no benchmark row'}]
    else:
        raise AssertionError('strict route accepted a missing effort cell')

    # Call the public catalog query with a deterministic live-state reader.
    server = SimpleNamespace(_source_guard=SimpleNamespace(check=lambda *a: None))
    with patch('plangraph.spawner.mcp.get_catalog_models', db.list_models), \
            patch('plangraph.spawner.mcp.leg_status', leg_status):
        queried = asyncio.run(MCPSpawnerServer.call_tool(server, 'plangraph_agent_query_catalog', {}))
    queried_models = {row['id']: row for row in queried['models']}
    assert queried_models[model]['benchmark_rows'] == list(rows.values())
    muse_row, = queried_models[muse.model]['benchmark_rows']
    if not (muse_row['source_url'] and muse_row['source_date']):
        pending = json.loads(Path(os.environ['PLANGRAPH_BENCH_PENDING']).read_text(encoding='utf-8'))
        entry = next(row for row in pending if row['subject'] == muse.model and row['effort'] == muse_row['effort'])
        assert {'source_url', 'source_date', 'source_identity'} <= set(entry['problems'])
        assert 'publisher record' in entry['reason']
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
