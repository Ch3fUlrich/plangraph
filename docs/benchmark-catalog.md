# Benchmark catalog provenance

`catalog_benchmarks` is keyed by the exact catalog model and effort. Published
snapshot rows carry `source_url` and ISO `source_date`; catalog queries and the
read-only planner loader retain both fields. `catalog_benchmark_sources.json`
freezes the exact upstream URL, page title (including effort), observed summary,
and supplied metrics. Published `source_url` values point directly to the upstream
model/effort page, not a self-citation to our own snapshot. `source_date` is the
observation date, not release date. The checked-in excerpt freezes what was observed;
the live page's rolling throughput can change after that observation.
Snapshot data records the evaluation version in `benchmark_source`.

Initialization adds provenance columns without dropping rows. Existing scores,
including operator edits, are not overwritten. Missing provenance is backfilled
only for rows matching every seed field; modified operator rows get no seed citation.
`tests/test_bench_rows_complete.py` requires coverage for active/spawnable variants;
missing benchmark rows fail the coverage test. Unknown scores must not be
interpolated from sibling efforts or used to change main's admission policy.

New measurements are recorded in `routing_support/catalog_benchmarks.json`.
Claude Fable 5.2 low/medium/high are catalog-only, not new planner legs; Claude
5.5 effort rows use separate exact ids. Sol and Luna cover all five `CX_EFFORTS`.
Unsuffixed cx and Gemini defaults resolve in `spawner/bench_entry.py`, retaining
the measured row's model id and citation rather than fabricating alias measurements.
The cx default is medium, matching `build_routing_envelope_headers`.
Additional model identities and providers are explicit neutral JSON data in
`catalog_benchmark_models.json`; seed imports never load planner leg policy.
All 25 existing benchmark rows, including MiMo pro/flash, remain byte-for-value
main measurements, with the original repository blob/line and seed date added.
This deliberately preserves reviewed scores, seat order and implement routing.
The repository citation verifies the historical seed, not an independently
reproduced AA evaluation; those historical scores remain upstream-unverifiable.
New identities carry `benchmark_only=1`: catalog queries expose them, while the
resolver's default reader excludes them. Explicit planner legs remain unchanged.
Gemini's unsuffixed AA page explicitly titles its high effort; DeepSeek's page
explicitly titles V4 Flash 0731 max. These canonical slugs are not guessed URLs.
Console Muse Contributor cites its exact provider data's DeepSWE v1.1 result
(75.4), not a distinct xhigh result. GPT-OSS medium cites OpenAI's model card
Table 3 SWE-Bench Verified score (52.6), in its own column, not DeepSWE or AA.
Haiku's unspecified served effort cannot be proved to match the cited max-effort
page: its row is explicitly `UNMEASURED`, all scores NULL. Main actually retains
the enabled prompt-only Haiku leg; this lane neither disables it nor changes tests.
`benchmark_options` excludes explicitly unknown composite/cost inputs but still
accepts main's historical minimal tables that omit cost columns altogether.
Unspecified Grok also has an explicitly unmeasured row: the cited source measures
xhigh, not the unspecified served effort. Its audited explicit-only admission and
automatic route ban are unchanged.
New main routes also get benchmark rows without changing their reviewed policy:
Argon high resolves to its existing exact catalog row; gateway Muse Contributor
has the same exact upstream DeepSWE evidence. Opus 4.6 thinking, unspecified
DeepSeek Flash, and stealth Big Pickle have no proven matching measurement.
Their rows explicitly record that limitation with NULL scores; Big Pickle's
provider-documentation URL is generic availability evidence, not a score source.

Every added upstream model URL was checked on 2026-11-12; model identity, effort
and scores were compared with the page (including embedded component metrics).
The 25 historical blob line references were also fetched and checked individually.
Haiku/Grok's unmeasured efforts and historical upstream reproducibility remain explicit
limitations, not claimed fixes. Query and missing-row regressions cover both doors.
Installed packages include all three benchmark JSON assets alongside main's
existing configuration JSON and SQL package data.
