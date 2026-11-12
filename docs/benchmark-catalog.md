# Benchmark catalog provenance

`catalog_benchmarks` is keyed by the exact catalog model and effort. Published
snapshot rows carry `source_url` and ISO `source_date`; catalog queries and the
read-only planner loader retain both fields. A live leaderboard's source date is
the dated snapshot observed, not the model release date or an invented publication
date. Snapshot data records the evaluation version in `benchmark_source`.

Initialization adds provenance columns without dropping rows. Existing scores,
including operator edits, are not overwritten or assigned a seed citation.
`tests/test_bench_rows_complete.py` requires coverage for active/spawnable variants;
an explicit `UNMEASURED` exception must remain disabled or explicit-only, never
removed (D-3828). Unknown scores must not be interpolated from sibling efforts.

New measurements are recorded in `routing_support/catalog_benchmarks.json`.
Historical October seeds remain uncited until verified; group commits replace
them with published measurements, rather than merely attaching plausible URLs.
