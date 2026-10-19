# Plangraph Spawner & Model Routing Architecture

Plangraph Spawner provisions, routes, and supervises autonomous agents across multi-model coding workflows. It combines a local WAL-mode SQLite benchmark catalog, a 5-rung tier priority ladder, fine-grained task complexity and risk measurement, OmniRoute ACP-first execution with full Windows Tailnet environment preservation, cross-family code review isolation, and a JSON-RPC 2.0 MCP server.

---

## 1. Architecture Overview

The spawner pipeline follows a deterministic, layered design:

```
[Task Request / Plan Item]
          │
          ▼
1. measure.py & risk.py
   ├── Complexity Bucketing: S0 (trivial/docs) .. S4 (cross-cutting, decomposition trigger)
   └── Risk Bands: low, normal, high, critical (governing reviewer counts & intelligence floors)
          │
          ▼
2. catalog_db.py (SQLite WAL)
   ├── 22 Verified Artificial Analysis v4.3.2 models, quota pools, and providers
   └── Exported role routes: l1-orchestrator, l2-worker, l2-reviewer, l3-driver
          │
          ▼
3. resolver.py
   ├── 5-Rung Tier Ladder: trial (0) < free (1) < credit (2) < subscription (3) < paid (4)
   ├── Utility Scoring: mimo/mimo-v2.6-pro prioritized over opencode-go/qwen3.8-max
   ├── Vendor Guardrails: Sonnet 5.5 max effort -> xhigh; Haiku 100K token price cliff
   └── Cross-Family Review: writer.family != rev1.family != rev2.family on shared combos
          │
          ▼
4. clients.py
   ├── OmniRoute ACP/Gateway first with offline native CLI fallback
   ├── Windows SystemRoot preservation (D-2090 BUG-B)
   └── OPENCODE_CONFIG_CONTENT injection with X-OmniRoute-* envelope headers
          │
          ▼
5. runner.py & mcp.py
   ├── Async non-blocking SpawnerRunner process pool (spawn, status, steer, cancel)
   ├── Response telemetry extraction: X-OmniRoute-Selected-Model / Selected-Family
   └── JSON-RPC 2.0 MCP server tools: plangraph_agent_*
```

---

## 2. 5-Rung Tier Priority Order & Quota Pools

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

---

## 3. Verified AA v4.3.2 Benchmark Catalog

The SQLite catalog (`src/plangraph/spawner/catalog_schema.sql`, `catalog_seed.py`, `catalog_db.py`) records 22 verified models benchmarked under Artificial Analysis v4.3.2 (October 2026):

| Rank | Model ID | Family | Tier | AA Intel | Verified tok/s | Direct tok/s | Task Cost ($) | Cache Hit | Role Routes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `agy/claude-opus-5-5-high` | claude | subscription | 57.6 | 98.0 | 45.0 | $1.82 | 88.0% | `l1-orchestrator` |
| 2 | `gemini/gemini-4-argon-high` | gemini | paid | 53.0 | 120.0 | 60.0 | $0.85 | 85.0% | `l1-orchestrator` |
| 3 | `agy/claude-sonnet-5-5-xhigh` | claude | subscription | 48.5 | 95.0 | 50.0 | $1.25 | 91.0% | `l1-orchestrator`, `l2-reviewer` |
| 4 | `agy/claude-sonnet-5-5-high` | claude | subscription | 46.8 | 100.0 | 52.0 | $1.12 | 91.0% | `l1-orchestrator`, `l2-worker`, `l2-reviewer` |
| 5 | `mimo/mimo-v2.6-pro` | mimo | subscription | **46.0** | **51.0** | **29.0** | **$0.13** | **92.4%** | `l1-orchestrator`, `l2-worker`, `l2-reviewer` |
| 6 | `opencode-go/qwen3.8-max` | qwen | subscription | 45.4 | 85.0 | 50.0 | $0.45 | 80.0% | `l1-orchestrator`, `l2-worker` |
| 7 | `opencode-go/deepseek-v4-flash-max` | deepseek | subscription | 45.2 | 210.0 | 110.0 | $0.22 | 84.0% | `l2-worker`, `l3-driver` |
| 8 | `opencode-go/glm-5.2-max` | glm | subscription | 45.1 | 90.0 | 55.0 | $0.35 | 82.0% | `l2-worker`, `l2-reviewer` |
| 9 | `opencode-go/muse-spark-1.3-contributor` | muse | subscription | 45.0 | 287.0 | 140.0 | $1.37 | 78.0% | `l2-worker` |
| 10 | `deepseek/deepseek-v4-flash-max` | deepseek | paid | 45.0 | 231.0 | 115.0 | $0.37 | 84.0% | `l2-worker`, `l3-driver` |
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

---

## 4. Cross-Family Review Protocol

To prevent self-review bias and single-model blindspots, Plangraph enforces strict cross-family isolation over shared OmniRoute combos:

$$\text{writer.family} \neq \text{reviewer}_1\text{.family} \neq \text{reviewer}_2\text{.family}$$

### Routing Envelope Headers

Requests dispatched through OmniRoute include routing envelope headers:

| Header | Description | Example |
|---|---|---|
| `X-OmniRoute-Task-Role` | Operational role | `implement`, `review`, `orchestrate`, `drive` |
| `X-OmniRoute-Risk` | Assessed risk band | `low`, `normal`, `high`, `critical` |
| `X-OmniRoute-Min-Intelligence` | Intelligence floor | `44.0` (normal), `45.0` (high), `46.0` (critical) |
| `X-OmniRoute-Required-Context` | Required token context | `32000`, `64000`, `128000` |
| `X-OmniRoute-Exclude-Family` | Comma-separated excluded families | `claude` (Rev 1), `claude,mimo` (Rev 2) |
| `X-OmniRoute-Author-Model` | ID of the author/writer model | `agy/claude-sonnet-5-5-high` |
| `X-OmniRoute-Effort` | Reasoning effort level | `low`, `medium`, `high`, `xhigh` |

### Gateway Response Telemetry

OmniRoute gateways return response headers reflecting the model actually selected and served:
- `X-OmniRoute-Selected-Model`: Canonical model identifier (e.g. `mimo/mimo-v2.6-pro`)
- `X-OmniRoute-Selected-Provider`: Active provider identifier (e.g. `mimo`)
- `X-OmniRoute-Selected-Family`: Canonical model family (e.g. `mimo`)

### Dual Reviewer Isolation Walkthrough

For a high-risk task where `agy/claude-sonnet-5-5-high` (family `claude`) is the writer:
1. **Writer**: Dispatched to `l2-worker`. Served model is `agy/claude-sonnet-5-5-high` (`claude`).
2. **Reviewer 1**: Dispatched to `l2-worker` with `X-OmniRoute-Exclude-Family: claude`. Gateway drops all Claude models; top remaining candidate is `mimo/mimo-v2.6-pro` (`mimo`).
3. **Reviewer 2**: Dispatched to `l2-worker` with `X-OmniRoute-Exclude-Family: claude,mimo`. Gateway drops both Claude and MIMO models; top remaining candidate is `opencode-go/qwen3.8-max` (`qwen`) or `opencode-go/muse-spark-1.3-contributor` (`muse`).

All 3 participants run over the **exact same** `l2-worker` combo definition without maintaining manual per-reviewer routes.

---

## 5. MCP Server & CLI Reference

### JSON-RPC 2.0 MCP Server (`src/plangraph/spawner/mcp.py`)

Run stdio MCP server:
```bash
python -m plangraph.spawner.mcp
```

#### Available MCP Tools

1. `plangraph_agent_spawn`:
   - `prompt` (string, required): Task prompt instructions.
   - `model_or_combo` (string, default `l2-worker`): Target model ID or combo route.
   - `role` (enum: `implement`, `review`, `orchestrate`, `drive`): Execution role.
   - `risk` (enum: `low`, `normal`, `high`, `critical`): Risk classification.
   - `dry_run` (boolean, default `false`): Plan command and telemetry without spawning subprocess.
   - `wait_seconds` (number, default `0.0`): Seconds to await process completion.
2. `plangraph_agent_status`:
   - `job_id` (string, optional): Specific job ID to inspect (returns all jobs if omitted).
3. `plangraph_agent_steer`:
   - `job_id` (string, required): Active job ID.
   - `message` (string, required): Steering instruction delivered to the agent process.
4. `plangraph_agent_cancel`:
   - `job_id` (string, required): Job ID to cancel.
5. `plangraph_agent_query_catalog`:
   - `combo` (string, optional): Filter by combo (e.g. `l2-worker`).
   - `family` (string, optional): Filter by model family (e.g. `mimo`, `claude`).
   - `min_intelligence` (number, optional): Minimum AA Intelligence floor.
6. `plangraph_agent_health`:
   - Reports spawner version, active/total jobs, catalog model counts, and gateway URL.

### SQLite Catalog DB CLI (`src/plangraph/spawner/catalog_db.py`)

- **Initialize & Seed Database**:
  ```bash
  python -m plangraph.spawner.catalog_db init-and-seed [--db-path PATH]
  ```
- **Export Deterministic JSON**:
  ```bash
  python -m plangraph.spawner.catalog_db export-json [--output PATH]
  ```
- **Export OmniRoute Combos**:
  ```bash
  python -m plangraph.spawner.catalog_db export-combos [--output PATH]
  ```

---

## 6. Host Bootstrap & AutoOS Delegation

Host binary and client prerequisites are delegated to AutoOS (`deploy/requirements-host.json`, `deploy/bootstrap.ps1`, `deploy/bootstrap.sh`) without duplicating package management code:

- Core runtimes: `git`, `python`, `nodejs`, `tailscale`, `omniroute`, `opencode-cli`
- Agent CLIs: `claude-code`, `qwen-code`, `gemini-cli`, `qoder-cli`, `agent-skill-links`

For host provisioning instructions, refer to [`deploy/README.md`](../deploy/README.md).
