"""Benchmark options for public entry points only."""
import os
from pathlib import Path
from plangraph.planner.routing import benchmark_rows_from_db, BenchDataError


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
    return dict(benchmark=rows, require_bench=bool(require_bench) or
                os.environ.get('PLANGRAPH_REQUIRE_BENCH') == '1')
