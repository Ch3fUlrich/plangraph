"""Benchmark options for public entry points only."""
import os
from pathlib import Path
import sqlite3
from plangraph.planner.benchmarks import benchmark_cells, benchmark_rows_from_db, BenchDataError

#: Explicit leg-id -> benchmark model_id (D2). Seeded benchmark rows are keyed
#: by provider-qualified model ids while spawner legs name bare harness models,
#: so nothing matched and benchmark p ranked mimo only. A leg absent here, or a
#: mapped id without a row, keeps today's "no benchmark p" (never invent one).
LEG_BENCHMARK_MODEL = {
    "qodercli-qwen3.8-flash": "qoder/qwen3.8-flash-next",
    "agy-sonnet-5.5": "agy/claude-sonnet-5-5-medium",
    "mimo-v2.6-pro": "mimo/mimo-v2.6-pro",
    "mimo-v2.6-flash": "mimo/mimo-v2.6-flash",
    "agy-opus-5.5": "agy/claude-opus-5-5-high",
}


def benchmark_options(require_bench=False, bench_db=None):
    paths = [bench_db] if isinstance(bench_db, str) else bench_db
    if not paths:
        default = Path(os.environ.get('PLANGRAPH_CATALOG_DB', '~/.cache/plangraph/spawner_catalog.db')).expanduser()
        if default.is_file():
            paths = [default]
    rows = None
    if paths:
        rows = {}
        for path in paths:
            found, errors = benchmark_rows_from_db(path)
            if errors:
                raise BenchDataError('; '.join(errors))
            rows.update(found)
        # Alias mapped rows under their leg ids so the plan lookup
        # (rows.get(leg.model, rows.get(leg.id))) reaches them.
        for leg_id, model_id in LEG_BENCHMARK_MODEL.items():
            row = rows.get(model_id)
            if row is not None and leg_id not in rows:
                rows[leg_id] = row
    return dict(benchmark=rows, require_bench=bool(require_bench) or
                os.environ.get('PLANGRAPH_REQUIRE_BENCH') == '1')


def catalog_benchmark_rows():
    """Expose canonical keys once, including benchmark models outside the legacy catalog."""
    rows = benchmark_options()['benchmark']
    if rows is None:
        from plangraph.routing_support.resolver import _get_default_catalog_db
        db = _get_default_catalog_db()
        cells = (benchmark_cells(db.conn) or {}) if db is not None else {}
        return [cell for group in cells.values() for cell in group]
    return [cell for key, row in sorted(rows.items()) if key == row['model_id']
            for cell in row.get('benchmark_rows', ())]


def catalog_query_benchmarks(models, combo, family, min_intel):
    from plangraph.routing_support.resolver import detect_model_family
    try:
        observations = catalog_benchmark_rows()
    except (BenchDataError, OSError, sqlite3.Error):
        return dict(benchmark_rows=[], benchmark_error='benchmark catalog unreadable')
    by_model = {}
    for row in observations:
        by_model.setdefault(row['model_id'], []).append(row)
    matched_ids = {model['id'] for model in models}
    for model in models:
        model['benchmark_rows'] = by_model.get(model['id'], [])
    return dict(benchmark_rows=[row for row in observations
        if (not combo or row['model_id'] in matched_ids)
        and (not family or detect_model_family(row['model_id']) == family.lower())
        and (min_intel is None or (row['origin'] == 'aa-index' and row['score'] is not None
                                   and row['score'] >= float(min_intel)))])
