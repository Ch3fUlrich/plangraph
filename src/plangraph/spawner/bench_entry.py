"""Benchmark options for public entry points only."""
import os
from pathlib import Path
from plangraph.planner.routing import benchmark_rows_from_db, BenchDataError

#: Explicit leg-id -> benchmark model_id (D2). Seeded benchmark rows are keyed
#: by provider-qualified model ids while spawner legs name bare harness models,
#: so nothing matched and benchmark p ranked mimo only. A leg absent here, or a
#: mapped id without a row, keeps today's "no benchmark p" (never invent one).
LEG_BENCHMARK_MODEL = {
    "qodercli-qwen3.8-flash": "qoder/qwen3.8-flash-next",
    "agy-gemini-3.8-flash": "agy/gemini-3.8-flash-tiered-high",
    "agy-gemini-4-argon": "gemini/gemini-4-argon-high",
    "agy-sonnet-5.5": "agy/claude-sonnet-5-5-medium",
    "mimo-v2.6-pro": "mimo/mimo-v2.6-pro",
    "mimo-v2.6-flash": "mimo/mimo-v2.6-flash",
    "agy-opus-5.5": "agy/claude-opus-5-5-high",
    # Unsuffixed cx routing uses the envelope's medium effort default.
    "cx-gpt-6.1-sol": "omniroute/cx-gpt-6.1-sol-medium",
    "cx-gpt-6-luna": "omniroute/cx-gpt-6-luna-medium",
}
# Resolve defaults at lookup time without minting measured catalog identities.
BENCHMARK_MODEL_ALIASES = {
    "omniroute/cx-gpt-6.1-sol": "omniroute/cx-gpt-6.1-sol-medium",
    "omniroute/cx-gpt-6-luna": "omniroute/cx-gpt-6-luna-medium",
    "gemini-3.8-flash": "agy/gemini-3.8-flash-tiered-high",
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
            # Old benchmark tables may omit cost entirely; main supports those.
            # An explicitly unknown score/cost is evidence, not a utility input.
            rows.update({key: row for key, row in found.items()
                         if all(key not in row or row[key] is not None for key in
                                ("aa_intelligence", "cost_per_task_usd"))})
        # Alias mapped rows under their leg ids so the plan lookup
        # (rows.get(leg.model, rows.get(leg.id))) reaches them.
        for leg_id, model_id in (LEG_BENCHMARK_MODEL | BENCHMARK_MODEL_ALIASES).items():
            row = rows.get(model_id)
            if row is not None and (leg_id not in rows or leg_id in BENCHMARK_MODEL_ALIASES):
                rows[leg_id] = row
    return dict(benchmark=rows, require_bench=bool(require_bench) or
                os.environ.get('PLANGRAPH_REQUIRE_BENCH') == '1')
