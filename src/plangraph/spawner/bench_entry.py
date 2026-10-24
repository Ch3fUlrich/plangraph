"""Benchmark options for public entry points only."""
import os
from plangraph.planner.routing import benchmark_rows_from_db, BenchRequired


def benchmark_options(require_bench=False, bench_db=None):
    paths = [bench_db] if isinstance(bench_db, str) else bench_db
    rows = None
    if paths:
        rows = {}
        for path in paths:
            found, errors = benchmark_rows_from_db(path)
            if errors:
                raise BenchRequired('; '.join(errors))
            rows.update(found)
    return dict(benchmark=rows, require_bench=bool(require_bench) or
                os.environ.get('PLANGRAPH_REQUIRE_BENCH') == '1')
