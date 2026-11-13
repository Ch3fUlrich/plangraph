## 4. 5-Rung Tier Priority Order & Quota Pools

Every model candidate is assigned a tier according to its billing and quota consumption profile:

| Rung | Tier | Description | Examples |
|---|---|---|---|
| **0** | `trial` | Zero-cost evaluation tiers | Experimental free trial keys |
| **1** | `free` | No marginal cost, daily replenished allowance | `agy/gemini-3.8-flash-tiered-high`, `qoder/qwen3.8-flash-next` |
| **2** | `credit` | Pre-funded non-recurring promotional balance | Promotional API balances |
| **3** | `subscription` | Fixed recurring subscription pools; strictly prioritized before paid APIs | `agy` (Claude/Gemini), `cxa` (Codex Luna), `mimo` (v2.6 pro/flash), `opencode-go`, `claude` |
| **4** | `paid` | Pay-as-you-go per-token APIs; used strictly as last resort | `deepseek` direct API, `gemini` direct API |

### Quota Pool Mechanics

1. **Antigravity (`agy`) Multi-Pool**:
   - Manages 3 independent pools: free daily tier, pro subscription seat allowance, and team pooled capacity.
   - Prevents starvation across concurrent planning and worker sessions.
2. **OpenCode-Go (`opencode-go`) Rolling Caps**:
   - Tracks sliding window usage caps per rolling 5-hour and 24-hour periods.
   - Models like `qwen3.8-max` and `glm-5.2-max` consume from subscription credits before failing over.
3. **MIMO Subscription & Cache Hit Discount**:
   - `mimo/mimo-v2.6-pro` and `mimo/mimo-v2.6-flash` operate under a deep subscription pool.
   - Features a **98% prompt caching discount** (`0.924` cache hit rate), reducing task cost to `$0.13` (Pro) and `$0.04` (Flash).
   - This economic and performance profile places `mimo/mimo-v2.6-pro` ahead of `opencode-go/qwen3.8-max` in utility ranking.

## 5. Verified AA v4.3.2 Benchmark Catalog

The SQLite catalog (`src/plangraph/spawner/catalog_schema.sql`, `catalog_seed.py`, `catalog_db.py`) records 22 verified models benchmarked under Artificial Analysis v4.3.2 (October 2026):

| Rank | Model ID | Family | Tier | AA Intel | Verified tok/s | Direct tok/s | Task Cost ($) | Cache Hit | Role Routes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `agy/claude-opus-5-5-high` | claude | subscription | 57.6 | 98.0 | 45.0 | $1.82 | 88.0% | `l1-orchestrator` |
| 2 | `gemini/gemini-4-argon-high` | gemini | paid | 53.0 | 120.0 | 60.0 | $0.85 | 85.0% | `l1-orchestrator` |
| 3 | `agy/claude-sonnet-5-5-xhigh` | claude | subscription | 48.5 | 95.0 | 50.0 | $1.25 | 91.0% | `l1-orchestrator`, `l2-reviewer` |
| 4 | `agy/claude-sonnet-5-5-high` | claude | subscription | 46.8 | 100.0 | 52.0 | $1.12 | 91.0% | `l1-orchestrator`, `l2-worker`, `l2-reviewer` |
| 5 | `mimo/mimo-v2.6-pro` | mimo | subscription | **46.0** | **51.0** | **29.0** | **$0.13** | **92.4%** | `l1-orchestrator`, `l2-worker`, `l2-reviewer` |
| 6 | `opencode-go/qwen3.8-max` | qwen | subscription | 45.4 | 85.0 | 50.0 | $0.45 | 80.0% | `l1-orchestrator`, `l2-worker` |
| 7 | `opencode-go/deepseek-v4.1-flash` | deepseek | subscription | **45.0** | **231.0** | **115.0** | **$0.26** | 84.0% | `l2-worker`, `l3-driver` |
| 8 | `opencode-go/glm-5.2` | glm | subscription | 45.1 | 90.0 | 55.0 | $0.35 | 82.0% | `l2-worker`, `l2-reviewer` |
| 9 | `opencode-go/muse-spark-1.3-contributor` | muse | subscription | 45.0 | 287.0 | 140.0 | $1.37 | 78.0% | `l2-worker` |
| 10 | `deepseek/deepseek-v4-flash-max` | deepseek | paid | **45.0** | **231.0** | **115.0** | **$0.26** | 84.0% | `l2-worker`, `l3-driver` |
| 11 | `claude/claude-haiku-4-5` | claude | subscription | 43.4 | 130.0 | 70.0 | $0.21 | 89.0% | `l2-worker` |
| 12 | `cxa/gpt-6-luna-high` | gpt | subscription | **38.4** | **180.0** | **95.0** | **$0.07** | **85.0%** | `l1-orchestrator`, `l2-worker`, `l2-reviewer`, `l3-driver` |
| 13 | `mimo/mimo-v2.6-flash` | mimo | subscription | 37.9 | **54.0** | **29.0** | **$0.04** | **92.4%** | `l2-worker`, `l3-driver` |
| 14 | `agy/gemini-3.8-flash-tiered-high` | gemini | free | 41.0 | 158.0 | 85.0 | $1.24 | 86.0% | `l1-orchestrator`, `l2-worker` |
| 15 | `qoder/qwen3.8-flash-next` | qwen | free | 40.0 | 175.0 | 90.0 | $0.05 | 81.0% | `l2-worker`, `l3-driver` |
| 16 | `cxa/gpt-6-astra-max` | gpt | subscription | 53.0 | 69.0 | 35.0 | $3.26 | 70.0% | *(Gated: Codex ACP luna-only)* |
| 17 | `cxa/gpt-6-sol-max` | gpt | subscription | 52.0 | 60.0 | 30.0 | $0.72 | 75.0% | *(Gated: Codex ACP luna-only)* |
| 18 | `cxa/gpt-6-sol-xhigh` | gpt | subscription | 50.5 | 65.0 | 32.0 | $0.58 | 75.0% | *(Gated: Codex ACP luna-only)* |
| 19 | `cxa/gpt-6-sol-high` | gpt | subscription | 48.0 | 72.0 | 38.0 | $0.44 | 75.0% | *(Gated: Codex ACP luna-only)* |
| 20 | `cxa/gpt-6-sol-medium` | gpt | subscription | 44.0 | 85.0 | 45.0 | $0.31 | 75.0% | *(Gated: Codex ACP luna-only)* |
| 21 | `cxa/gpt-6-sol-low` | gpt | subscription | 40.0 | 110.0 | 60.0 | $0.20 | 75.0% | *(Gated: Codex ACP luna-only)* |
| 22 | `cxa/gpt-6-luna-low` | gpt | subscription | 34.0 | 220.0 | 120.0 | $0.03 | 85.0% | `l3-driver` |

### Offline benchmark data contract (BENCH-DATA-TEST, slice 1)

`tests/test_benchmark_data_contract.py` checks the actual
`routing_support/catalog_seed.py::SEED_BENCHMARKS` data against
`catalog_data.py::SEED_MODELS` and `planner/legs.py::spawnable_legs()` (routed
**and** explicit-only legs). The spawner catalog modules are compatibility
aliases for the routing-support modules. This slice changes no row values,
SQLite schema, runtime admission, ranking, or availability.

- Every `status == "active"` catalog model and every non-permanently-disabled
  spawnable leg requires an exact model-id × effort benchmark row. Availability
  flags do not waive coverage. Temporary pauses do not waive eventual coverage;
  the test passes positive infinity to `leg_active`, treating every expiring
  pause as expired without reading the clock.
- Bare leg models are qualified with their harness, as in the benchmark reader.
  Effort is the explicit leg/row `effort`, otherwise the catalog's
  `parameter_effort`, otherwise a recognized effort suffix. Null is a distinct
  unspecified effort, not a wildcard. There is no cross-provider or fuzzy
  model matching; identity aliases do not supply coverage for another row.
- Each row requires a nonempty HTTP(S) `source_url`, an ISO `source_date`
  (`YYYY-MM-DD`), and a `source_record` object containing publisher `title`
  and/or `excerpt` text. That text must name the exact `model_id` or a nonempty
  string explicitly listed in the row's `aliases` array (optional, defaults to
  empty). Matching is case-insensitive and token-bounded: a different release
  or effort suffix cannot match a shorter id; sentence-final periods are allowed.
  Parenthesized qualifiers and whitespace-separated effort/version suffixes
  extend the identity and must be included in an explicitly listed alias.
  Publisher fields are checked separately, so their boundary cannot form a suffix.
  Record metadata and the URL do not count as publisher text. `updated_at` is
  not a source date and a generic
  `benchmark_source` label is not identity evidence.

The **only** pending list is
`configuration/benchmark_data_pending.json`: each entry names a subject,
effort, specific failing checks, and a reason. Missing-leg subjects bind both
the leg id and its qualified model id, so a retargeted leg cannot reuse a waiver.
Its initial 58 entries cover 33 missing-row cases and the three provenance gaps
on each of 25 existing rows
(108 individual violations). An independently frozen set in the test bounds
the exact allowed violations. Its literal count and SHA-256 are pinned separately
in `tests/fixtures/benchmark_data_ceiling.json`, not in the legacy-table module
and not derived from the pending file. Editing the tables and any module-local
pins together still fails the independent fingerprint check. Canonical ordering
distinguishes null from named efforts without comparing those types directly.
A separate test compares the pending file with that
ceiling, allowing only removals, so additions and same-size substitutions fail.
Resolved violations must be removed; provenance debt cannot hide missing rows.
Do not extend or regenerate the frozen ceiling when fixing data.

All verification is local: the test blocks network and subprocess entry points
and reads neither Git history nor a live catalog. Synthetic mutations prove
that missing rows, null/omitted citations, wrong effort, unrelated source text,
and pending-list growth fail, including with Python optimization enabled.
Contract checks raise explicitly rather than relying on removable `assert`
statements. This is a structural/identity contract, **not**
verification that publisher bytes or scores are authentic. Independent source
collection, review, and any row corrections belong to a later slice; this slice
does not invent citations, aliases, or evidence for legacy rows.

D-2739 adds publisher identity-only rows for the free review registrations and
the corrected direct DeepSeek path in `config/review_benchmarks.json`.
Their source dates record collection, not publication. Unknown capability,
speed and task-cost metrics remain null. The offline structural/identity contract
accepts their exact sourced keys; the numeric SQLite seed excludes them rather
than substituting fabricated zeros or another provider's score. Consequently
`--require-bench` still refuses these routes until genuine numeric measurements
are supplied. No frozen ceiling or admission assertion was relaxed.

### Key Ranking & Guardrail Invariants

- **`mimo/mimo-v2.6-pro` vs `opencode-go/qwen3.8-max`**:
  `mimo/mimo-v2.6-pro` has `46.0` AA Intelligence vs `45.4` for Qwen, verified token speed of `51 tok/s` (with `29 tok/s` direct API floor), lower task cost ($0.13 vs $0.45), higher cache hit rate (`92.4%` vs `80.0%`), and access to a deeper subscription pool. Utility scoring deterministically ranks `mimo/mimo-v2.6-pro` ahead of Qwen in both `l1-orchestrator` and `l2-worker`.
- **`mimo/mimo-v2.6-flash` Performance**:
  Measured at `54 tok/s` overall verified, `29 tok/s` direct API, `75.4` CyberBench score, and only `$0.04/task` cost, making it the premier budget fast-worker for S0/S1 tasks.
- **Codex ACP Luna-Only Policy**:
  Codex ACP (`cxa`) currently supports `cxa/gpt-6-luna-high` (`38.4` Intel, `180 tok/s`, `$0.07/task`) as the sole active production model. Models under `cxa/gpt-6-astra-max` and `cxa/gpt-6-sol-*` are gated (`status = 'gated'`, `available = 0`) to avoid silent failure or API timeouts.
- **Claude Sonnet 5.5 Effort Clamping**:
  Anthropic Claude Sonnet 5.5 does not support effort level `max`. `clamp_effort()` automatically transforms requested effort `max` $\rightarrow$ `xhigh` for all `claude-sonnet-5-5*` models.
- **Claude Haiku Price Cliff**:
  `claude/claude-haiku-4-5` imposes a steep price increase for contexts above 100K tokens. When `need_tokens > 100000`, the resolver strictly drops Haiku from candidate pools.
- **DeepSeek Flash v1017**:
  The updated DeepSeek Flash v1017 snapshot reaches an Artificial Analysis Intelligence Index of `45.0` at `$0.26/task` cost and `231 tok/s` verified throughput. Available in both `opencode-go` subscription (`opencode-go/deepseek-v4.1-flash`, $U_{\text{core}} = 0.90$) and direct paid PAYG (`deepseek/deepseek-v4-flash-max`, $U_{\text{core}} = -2.60$). Subscription DeepSeek Flash beats Qwen Max ($U_{\text{core}} = -0.60$) on cost efficiency while providing strong code reasoning.
