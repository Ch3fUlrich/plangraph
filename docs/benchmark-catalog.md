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
Claude Fable 5.2 low/medium/high are explicit-only; Claude 5.5 effort rows use
separate exact ids. Haiku 4.5 has no published composite score in this snapshot.
GPT-6.1 Sol and Luna cover all five `CX_EFFORTS`; the unsuffixed cx leg aliases
the medium row, matching `build_routing_envelope_headers`' default effort.
MiMo pro/flash use the published model-page composite scores (30/27). Provider
throughput/cost describe the cited endpoint, not a claim about subscription burn.
Historical October seeds remain uncited until verified; group commits replace
them with published measurements, rather than merely attaching plausible URLs.
