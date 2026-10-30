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


def benchmark_rows_from_db(path):
    """Return (model-id rows, errors) without creating or modifying a database.

    Prefer the catalog table when both supported tables exist. Duplicate model rows
    are counted in ``benchmark_row_count``; the first row supplies ranking metrics.
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
            for raw in conn.execute(f'SELECT * FROM "{table}"'):
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
            return result, []
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {}, [f"{path}: cannot read benchmark database: {exc}"]
