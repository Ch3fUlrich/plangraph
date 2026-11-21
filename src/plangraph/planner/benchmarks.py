"""Benchmark rows for :func:`plangraph.planner.routing.plan_spawn` (split from routing.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from plangraph.routing_errors import SpawnRefused


class BenchmarkError(SpawnRefused):
    """Benchmark policy or storage failure."""


class BenchRequired(BenchmarkError):
    """Strict benchmark policy refused one or more candidate legs."""

    def __init__(self, message, legs=()):
        super().__init__(message)
        self.gaps = [leg for leg in legs if isinstance(leg, dict)]
        self.legs = [leg['leg'] if isinstance(leg, dict) else leg for leg in legs]


class BenchDataError(BenchmarkError):
    """Benchmark storage could not be read; not missing model coverage."""


# Table-specific aliases to the resolver's canonical columns. No fuzzy model aliases.
BENCHMARK_COLUMNS = {
    "catalog_benchmarks": {"model_id": "model_id"},
    "model_benchmarks": {"model_id": "model_id", "intelligence": "aa_intelligence",
                         "tok_per_s": "verified_tok_per_s"},
}


def benchmark_cells(conn, model_id=None):
    """Preferred observation of each model, effort and harness in a current catalog."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='catalog_benchmark_rows'").fetchone() is None:
        return None
    query = 'SELECT * FROM catalog_benchmark_rows'
    params = ()
    if model_id is not None:
        query += ' WHERE model_id = ?'
        params = (model_id,)
    result = {}
    for row in conn.execute(query + ' ORDER BY model_id, effort, harness', params):
        result.setdefault(row['model_id'], []).append(dict(row))
    return result


def _utility_row(cell):
    row = dict(cell, aa_intelligence=cell['score'])
    if row.get('cost_per_task_usd') is None:
        row.pop('cost_per_task_usd', None)
    return row


def benchmark_for_leg(rows, leg):
    row = rows.get(leg.model, rows.get(leg.id))
    if row is None or 'benchmark_rows' not in row:
        return row
    cells = row['benchmark_rows']
    effort = leg.effort or ''
    exact = next((cell for cell in cells if cell['effort'] == effort and cell['harness'] == leg.harness), None)
    if exact is None:
        exact = next((cell for cell in cells if cell['legacy'] and
                      cell['effort'] in ('', effort) and cell['harness'] in ('', leg.harness)), None)
    return _utility_row(exact) if exact is not None and exact['score'] is not None else None


def benchmark_rows_from_db(path):
    """Return (model-id rows, errors) without creating or modifying a database.

    Prefer the current catalog view. Older databases expose raw benchmark tables.
    """
    try:
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in BENCHMARK_COLUMNS if t in tables), None)
            if table is None:
                return {}, [f"{path}: missing benchmark table (catalog_benchmarks or model_benchmarks)"]
            result = {}
            cells = benchmark_cells(conn)
            if cells is not None:
                for model, values in cells.items():
                    first = next((row for row in values if row['legacy']), values[0])
                    result[model] = dict(_utility_row(first), benchmark_rows=values,
                                         benchmark_row_count=len(values))
                raw_rows = ()
            else:
                raw_rows = conn.execute(f'SELECT * FROM "{table}"')
            for raw in raw_rows:
                row = dict(raw)
                for original, canonical in BENCHMARK_COLUMNS[table].items():
                    if original in row and canonical not in row:
                        row[canonical] = row[original]
                model = row.get("model_id")
                if not isinstance(model, str) or not model:
                    return {}, [f"{path}: {table} row missing model_id"]
                if model in result:
                    result[model]["benchmark_row_count"] += 1
                else:
                    result[model] = dict(row, benchmark_row_count=1)
            # Bare harness models use the exact provider-qualified catalog key.
            # Preserve explicit rows; absent measurements must remain gaps.
            from .legs import DEFAULT_LEGS
            for leg in DEFAULT_LEGS:
                key = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
                if key in result:
                    result.setdefault(leg.model, result[key])
            return result, []
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {}, [f"{path}: cannot read benchmark database: {exc}"]
