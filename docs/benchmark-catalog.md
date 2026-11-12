# Benchmark catalog provenance

`catalog_benchmarks` is keyed by the exact catalog model and effort. Published
snapshot rows carry `source_url` and ISO `source_date`; catalog queries and the
read-only planner loader retain both fields. `catalog_benchmark_sources.json`
freezes the exact upstream URL, page title (including effort), observed summary,
and supplied metrics. The seed reader resolves each upstream URL to its observation
in that content-addressed snapshot. Published `source_url` values address that immutable Git blob
and the exact model entry; `source_date` is the observation date, not release date.
The upstream live page remains in the evidence, not masquerading as a snapshot.
Snapshot data records the evaluation version in `benchmark_source`.

Initialization adds provenance columns without dropping rows. Existing scores,
including operator edits, are not overwritten or assigned a seed citation.
`tests/test_bench_rows_complete.py` requires coverage for active/spawnable variants;
an explicit `UNMEASURED` exception must remain disabled or explicit-only, never
removed (D-3828). Unknown scores must not be interpolated from sibling efforts.

New measurements are recorded in `routing_support/catalog_benchmarks.json`.
Claude Fable 5.2 low/medium/high are explicit-only; Claude 5.5 effort rows use
separate exact ids. GPT-6.1 Sol and Luna cover all five `CX_EFFORTS`.
Unsuffixed cx and Gemini defaults resolve in `spawner/bench_entry.py`, retaining
the measured row's model id and citation rather than fabricating alias measurements.
The cx default is medium, matching `build_routing_envelope_headers`.
Additional model identities and providers are explicit neutral JSON data in
`catalog_benchmark_models.json`; seed imports never load planner leg policy.
MiMo pro/flash use the published model-page composite scores (30/27). Provider
throughput/cost describe the cited endpoint, not a claim about subscription burn.
Only metrics explicitly supplied by the new observations are overlaid. All 22
historical rank values and unrelated coding/agentic/terminal/cache/provider-speed
fields remain unchanged. Rows without a replacement (including Haiku 4.5 and
gated GPT-6 Sol efforts) remain byte-for-value historical seeds, cited to the
exact original repository blob/line and seed date, not a claimed new upstream
evaluation. `benchmark_source` identifies the retained fields' original citation.
Gemini's unsuffixed AA page explicitly titles its high effort; DeepSeek's page
explicitly titles V4 Flash 0731 max. These canonical slugs are not guessed URLs.
Muse Contributor now cites its exact provider data's DeepSWE v1.1 result (75.4),
not the distinct xhigh SciCode/throughput row. DeepSeek, GLM and Kimi K3 component
rows retain a NULL composite; `benchmark_options`
does not treat component evidence or unknown task costs as a utility input. Unspecified
Haiku efforts and GPT-OSS medium are unmeasured and disabled, not removed.
Unspecified Grok retains its existing audited explicit-only admission and automatic
route ban; benchmark coverage does not disable this unrelated manual workflow.
