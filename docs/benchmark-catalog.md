# Benchmark catalog provenance

`catalog_benchmarks` contains one current row per exact catalog identity/effort.
`catalog_benchmarks.json` supplies all 60 rows, including the 25 refreshed historical
identities. `catalog_benchmark_sources.json` freezes upstream titles, excerpts,
component evidence, scores and dates. Historical metrics in its `legacy` section
are upgrade fingerprints and the router's existing calibration, never current
measurements or source citations. Every non-NULL source URL was fetched and checked
on 2026-11-12 against the model, effort, score and evaluation. Generic availability
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
token limits on `muse-amber` remain zero/unknown, not invented capabilities.
All identities pass registry tagging; the rows require four provider registrations.
Seed imports do not load planner policy. All three
JSON assets are packaged with the existing SQL/config assets. Coverage tests check
every enabled spawnable leg and active catalog identity plus the independently
item-required `muse-amber`; omission or catalog inactivity cannot hide that name.
An injected future leg without a benchmark row fails coverage.

## Exact components and alias handling

Muse Spark Contributor has only the exact upstream DeepSWE v1.1 pass@1 result
(75.4, max effort, mini-swe-agent), not an invented composite, cost or xhigh score.
GPT-OSS medium has SWE-Bench Verified 52.6 from OpenAI's model card Table 3 in its
own column. Haiku 5.5 has Terminal-Bench 4.0 score 39.2 from Anthropic's performance table;
the publisher does not specify effort, so no max-effort composite is attributed.

Muse cites Meta's publisher scorecard, checked visually against its methodology,
not OpenCode's second-hand model-data mirror. The optional live audit verifies
every cited row against fetched publisher bytes (including exact SciCode values),
and rejects unrelated pages even when they return HTTP 200:
Set `PLANGRAPH_VERIFY_BENCHMARK_SOURCES` to `1` and run
`python3 -m pytest -q tests/test_bench_catalog_evidence.py -k live_publisher_sources`.

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

## Unresolved source requirements (not claimed complete)

Eight exact identities have no proven matching measurement. They have dated,
explicit `UNMEASURED` rows with **NULL source URLs and scores**, not misleading
generic or sibling citations:

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

These are unresolved acceptance gaps, not verified benchmarks. The disabled and
unavailable `omniroute/gemini-3.8-flash` route still has no row. No routing policy
is changed to conceal missing evidence. No gate waiver is needed: migration and
snapshot readers live in the existing catalog seed/data owners, and CONTRIBUTING
and the new-code allowlist remain byte-identical to main.
