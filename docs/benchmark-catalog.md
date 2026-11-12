# Benchmark catalog provenance

`catalog_benchmarks` contains one current row per exact catalog identity/effort.
`catalog_benchmarks.json` supplies all 60 rows, including the 25 refreshed historical
identities. `catalog_benchmark_sources.json` freezes upstream titles, excerpts,
component evidence, scores and dates. Historical metrics in its `legacy` section
are upgrade fingerprints and the router's existing calibration, never current
measurements or source citations. The fix-round audit fetched each of the 43 distinct
non-NULL source URLs (52 rows) on 2026-11-12 and saved a response SHA-256, checked date
and source note per row. AA excerpts now bind slug, model name, effort and SciCode
inside one publisher model record, not the first component in a comparison array.
Generic availability
pages and this project's own repository are not benchmark evidence. The external
Big Pickle evaluation is explicitly a self-report, not an authoritative result.

Initialization refreshes only exact old seed rows, including the PR's prior blob
citations and fingerprinted prior PR snapshots. Operator-modified measurements and provenance remain unchanged. New
observations replace the entire historical measurement, clearing unsupported
components, cache statistics and throughput rather than relabelling them with a
new source. Routes, admission rules and explicit planner legs do not change.
`metric_view="benchmark"` reads current observations. The default `routing` view
preserves the existing v4-calibrated thresholds/utility for unchanged seed rows,
labels `metric_view`, retains the actual current observation under `benchmark`,
and clears top-level provenance rather than mis-citing v5 for v4 scores. Operator
measurements are never replaced with calibration. Raw observations remain in
`catalog_benchmarks`; exporting the catalog exports observations, not calibration.
General query_catalog requests show current observations; combo queries retain
routing thresholds and include the observation and provenance under `benchmark`.
This prevents the data refresh from silently moving existing seats or crashing
on NULL composites. Recalibrating routing to AA v5 is a separate policy change.

All published November observations reach the database, including MiMo pro/flash,
DeepSeek V4.1 Flash, Argon, Gemini tiered high (31), and cxa Luna high (21).
Provider-qualified and bare Claude efforts share the same exact upstream
observations, so they cannot retain contradictory scores for the same effort.
`master_rank` is retained historical metadata, not a current evaluation ranking.

New identities are explicit neutral data in `catalog_benchmark_models.json` and
carry `benchmark_only=1`. query_catalog includes these rows; the resolver's
default reader excludes them. They are unavailable, inactive for admission, and
carry an explicit reason even when their identity status is active. Unknown
token limits remain zero/unknown wherever no frozen capability source exists, not
invented capabilities. Asset admission fields are authoritative: all benchmark-only
identities explicitly declare `available=0` and an unavailable reason; the reader
rejects inconsistent data instead of silently replacing it. Effort is a separate
parameter, never part of `upstream_id`. This is catalog identity normalization,
not a claim that an unverified gateway accepts that request.
All identities pass registry tagging; the rows require four provider registrations.
Seed imports do not load planner policy. All three
JSON assets are packaged with the existing SQL/config assets. Coverage tests check
every enabled spawnable leg and active catalog identity plus the independently
item-required `muse-amber`; omission or catalog inactivity cannot hide that name.
An injected future leg without a benchmark row fails coverage.

## Exact components and alias handling

Muse Spark Contributor has only the exact upstream DeepSWE v1.1 task-pass-rate result
(75.4, max effort, mini-swe-agent), not an invented composite, cost or xhigh score.
GPT-OSS medium has SWE-Bench Verified 52.6 from OpenAI's model card Table 3 in its
own column. Haiku 5.5 has Terminal-Bench 4.0 score 39.2 from Anthropic's performance table;
the publisher does not specify effort, so no max-effort composite is attributed.

Muse cites Meta's publisher scorecard, checked visually against its methodology,
with a frozen transcription of the exact column, row and 75.4 cell plus its image
SHA-256. The score is not inferred from the image URL text or
OpenCode's second-hand model-data mirror. The optional live audit verifies
every cited row against fetched publisher bytes (including exact SciCode values),
and rejects unrelated pages even when they return HTTP 200:
Set `PLANGRAPH_VERIFY_BENCHMARK_SOURCES` to `1` and run
`python3 -m pytest -q tests/test_bench_catalog_evidence.py -k live_publisher_sources`.
The audit was actually run in this fix round; the default suite remains offline.
Mutable publisher speed/cost observations may change after the frozen check date.
Canonical AA URLs can omit the default effort suffix: the publisher model record's
`effort.slug` and title, not a guessed `-high`/`-max` URL, establish that effort.
Main had no benchmark URLs to reuse; its model IDs are not publisher slug evidence.
All AA citations use `/models/<hyphenated-slug>`; other publishers keep their own
specific article, table, pinned result file or scorecard form.

Three copied SciCode values were corrected from identity-bound publisher records:
GPT-6.1 Sol max is 54.1666666666667, Luna max 54.6296296296296 and Astra max
56.4814814814815. Repeated values on MiMo, DeepSeek/Luna high, Gemini medium/Sol
xhigh, Sol low/medium and Kimi/Fable high each have separate exact model records.
SciCode itself is marked under review by AA; these are reported observations, not
an endorsement of the benchmark. Argon's task cost also changed from 9.95 to 4.97;
the whole-row sweep refreshed current speed/cost excerpts consistently.

Big Pickle cites a pinned result file from a reproducible SWE Atlas QnA run (50.81%,
63/124, 2026-08-11), in `swe_atlas_qna`, not DeepSWE or SWE-Bench Verified. This
is a **self-reported single trial**, not Scale-verified, with a mutable stealth
identity, reduced sandbox resources and unscored rubrics (strict lower bound
61/124 = 49.2%). Its date is the evaluation date, not the November audit date.

Utility uses the labelled historical calibration for unchanged historical seed
identities (including component-only DeepSeek) without inventing missing current
AA scores or cost. New NULL-only identities such as muse-amber still cannot be
scored; that requirement remains unresolved. Minimal tables may omit cost.
Model corrections use saved seed versions for three-way, per-column refreshes:
unchanged seed cells update; operator edits survive, not a hardcoded Muse patch.
Alias expansion never overwrites any
existing database identity, measured or unmeasured, across all input databases.
Missing cx aliases resolve to medium only when no database row has that identity.
The unsuffixed Luna row is explicitly unmeasured; its leg-id lookup can use medium
without presenting that measurement as an unsuffixed catalog score.

## Unmeasured identities and remaining evidence limitations

Eight exact identities have no proven matching measurement. They have dated,
explicit `UNMEASURED` rows with **NULL source URLs and scores**, not misleading
generic or sibling citations. For an unmeasured leg, "source + date" means the
reason in `benchmark_source`/the source excerpt and `source_date` (date checked),
plus the explicit `checked_on` and dated source note in the evidence asset:

| Identity | Evidence gap |
| --- | --- |
| `muse-amber` | No authoritative identity/source found. Muse Ember 1.0 exists, but the names are not proven aliases; Muse Spark Contributor is also distinct. The item name is retained explicitly, not silently replaced. |
| `claude-opus-4-6-thinking` | The published max-effort page does not establish the served thinking effort. |
| `omniroute/gc/grok-4.7` | The leg specifies no effort; published high/xhigh evaluations cannot establish its served effort. |
| `omniroute/deepseek-flash` | The gateway alias does not prove release or effort. |
| `claude/claude-haiku-4-5` | The historical route does not identify reasoning vs non-reasoning evaluation. |
| `omniroute/cx-gpt-6-luna` | Historical seed copied a high-effort score; the unsuffixed route defaults to medium, not a separately measured identity. |
| `openrouter/free` | Dynamic router, not one fixed model/effort. |
| `omniroute/deepseek-direct-flash` | New main worker alias does not establish release or effort. |

These remain unmeasured, not verified benchmarks; no generic or unverifiable URL
is kept as a score citation. Big Pickle's lack of an authoritative evaluation is
still an evidence limitation, not fixed by pinning a third-party self-report.
The disabled and
unavailable `omniroute/gemini-3.8-flash` route still has no row. No routing policy
is changed to conceal missing evidence. No gate waiver is needed: migration and
snapshot readers live in the existing catalog seed/data owners, and CONTRIBUTING
and the new-code allowlist remain byte-identical to main.

## Capability and insertion invariants

`capabilities` in the evidence asset freezes the inherited main catalog limits and
their exact revision reference. These are catalog limits, **not newly measured
vendor maxima**. Opus, Sonnet, Gemini Flash, Argon, Muse Contributor and Luna use
identical limits across harness prefixes/efforts. The pre-existing Luna 872000 vs
900000 and disabled Gemini 128000 vs 1048576 inconsistencies were reconciled too.
New identities without a frozen capability source use 0 for both unknown limits,
with a dated explanation; there is no uniform 900000/64000 placeholder template.
Historical differences in `prior_models` are upgrade fingerprints only and must
not be rewritten as current capabilities. Saved seed versions preserve operator
edits while refreshing prior lane-owned cells.

Seed inserts ignore only duplicate `model_id` conflicts, not CHECK, NOT NULL or
foreign-key errors. Whole-asset tests enforce citations, exact component identity,
default effort evidence, checked/reason fields, capability consistency and unknown
limits, explicit admission, upstream normalization and loud invalid-row failures.
