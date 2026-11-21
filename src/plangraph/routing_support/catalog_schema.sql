-- Plangraph Spawner Catalog SQLite Schema
-- Compatible with PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS catalog_quota_pools (
    pool_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('trial', 'free', 'credit', 'subscription', 'paid')),
    window_type TEXT NOT NULL,
    pool_burn_multiplier REAL NOT NULL DEFAULT 1.0,
    cache_hit_discount REAL NOT NULL DEFAULT 0.0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS catalog_providers (
    provider_id TEXT PRIMARY KEY,
    omniroute_id TEXT,
    tier TEXT NOT NULL CHECK (tier IN ('trial', 'free', 'credit', 'subscription', 'paid')),
    transport TEXT NOT NULL CHECK (transport IN ('acp', 'gateway', 'cli', 'both')),
    gateway_mode TEXT NOT NULL DEFAULT 'omniroute_acp',
    available INTEGER NOT NULL DEFAULT 1,
    unavailable_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_clients (
    client_id TEXT PRIMARY KEY,
    binary_name TEXT NOT NULL,
    transport TEXT NOT NULL,
    gateway_first INTEGER NOT NULL DEFAULT 1,
    tier TEXT NOT NULL CHECK (tier IN ('trial', 'free', 'credit', 'subscription', 'paid')),
    available INTEGER NOT NULL DEFAULT 1,
    unavailable_reason TEXT
);

CREATE TABLE IF NOT EXISTS catalog_models (
    model_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES catalog_providers(provider_id),
    upstream_id TEXT NOT NULL,
    family TEXT NOT NULL,
    parameter_effort TEXT,
    max_safe_effort TEXT,
    effort_warning TEXT,
    context_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    price_cliff_tokens INTEGER,
    quota_pool_id TEXT REFERENCES catalog_quota_pools(pool_id),
    pool_burn_weight REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL CHECK (status IN ('active', 'upgrade', 'gated', 'retired')),
    available INTEGER NOT NULL DEFAULT 1,
    unavailable_reason TEXT,
    supports_reasoning INTEGER NOT NULL DEFAULT 1,
    supports_tool_calling INTEGER NOT NULL DEFAULT 1,
    supports_vision INTEGER NOT NULL DEFAULT 0,
    trains_on_prompts INTEGER NOT NULL DEFAULT 0,
    -- Runner harness that serves this id (planner/legs.py HARNESSES); NULL = none.
    harness TEXT CHECK (harness IS NULL OR harness IN ('qodercli', 'opencode', 'agy')),
    -- Review input: own tools, or a diff the spawner inlines (D-3134/D-3173).
    tool_mode TEXT CHECK (tool_mode IS NULL OR tool_mode IN ('tools', 'inline-only'))
);

CREATE TABLE IF NOT EXISTS catalog_benchmarks (
    model_id TEXT PRIMARY KEY REFERENCES catalog_models(model_id) ON DELETE CASCADE,
    master_rank INTEGER,
    aa_intelligence REAL NOT NULL,
    coding_index REAL,
    agentic_index REAL,
    terminal_bench REAL,
    deepswe REAL,
    frontier_code REAL,
    scicode REAL,
    cyberbench REAL,
    verified_tok_per_s REAL NOT NULL,
    direct_api_tok_per_s REAL,
    alt_provider_tok_per_s REAL,
    ttft_seconds REAL,
    cost_per_task_usd REAL NOT NULL,
    cache_hit_rate REAL,
    benchmark_source TEXT NOT NULL DEFAULT 'AA-v4.3.2-2026-10',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_benchmark_measurements (
    model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
    effort TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL CHECK (length(trim(harness)) > 0),
    score REAL CHECK (score BETWEEN 0 AND 100),
    score_se REAL CHECK (score_se >= 0),
    n_runs INTEGER CHECK (n_runs >= 0),
    steps REAL CHECK (steps >= 0),
    cost_per_task_usd REAL CHECK (cost_per_task_usd >= 0),
    tokens_per_task REAL CHECK (tokens_per_task >= 0),
    imputed INTEGER NOT NULL DEFAULT 0 CHECK (imputed IN (0, 1)),
    effort_channel TEXT,
    index_version TEXT,
    source_url TEXT,
    source_date TEXT CHECK (source_date IS NULL OR julianday(source_date) IS NOT NULL),
    source_record TEXT,
    measured_at TEXT CHECK (measured_at IS NULL OR julianday(measured_at) IS NOT NULL),
    origin TEXT NOT NULL CHECK (origin IN ('own', 'deepswe', 'cursorbench', 'frontierswe', 'aa-index')),
    PRIMARY KEY (model_id, effort, harness, origin)
) STRICT;

CREATE TABLE IF NOT EXISTS catalog_benchmark_refresh_sources (
    source TEXT PRIMARY KEY, url TEXT NOT NULL, etag TEXT, last_modified TEXT,
    attribution TEXT, revision INTEGER NOT NULL DEFAULT 1
) STRICT;

CREATE TABLE IF NOT EXISTS catalog_benchmark_openrouter_models (
    id TEXT PRIMARY KEY, canonical_slug TEXT, index_version TEXT,
    prompt_price REAL, completion_price REAL
) STRICT;

CREATE TABLE IF NOT EXISTS catalog_benchmark_price_inputs (
    model_id TEXT NOT NULL, effort TEXT NOT NULL, harness TEXT NOT NULL,
    openrouter_id TEXT NOT NULL,
    prompt_tokens REAL CHECK (prompt_tokens >= 0),
    completion_tokens REAL CHECK (completion_tokens >= 0),
    reference_prompt_price REAL CHECK (reference_prompt_price >= 0),
    reference_completion_price REAL CHECK (reference_completion_price >= 0),
    PRIMARY KEY (model_id, effort, harness)
) STRICT;

CREATE TABLE IF NOT EXISTS catalog_benchmark_refresh_flags (
    model_id TEXT NOT NULL, effort TEXT NOT NULL, harness TEXT NOT NULL,
    stale_reason TEXT, repriced_cost REAL, price_disagreement INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model_id, effort, harness)
) STRICT;

-- Legacy observations identify the model and effort, with no measured harness.
DROP VIEW IF EXISTS catalog_benchmark_rows;
CREATE VIEW catalog_benchmark_rows AS
WITH observations AS (
    SELECT model_id, effort, harness, score, score_se, n_runs, steps, cost_per_task_usd,
           tokens_per_task, imputed, effort_channel, index_version, source_url, source_date,
           source_record, measured_at, origin, 0 AS legacy, NULL AS verified_tok_per_s
    FROM catalog_benchmark_measurements
    UNION ALL
    SELECT b.model_id, COALESCE(m.parameter_effort, ''), '',
           b.aa_intelligence, NULL, NULL, NULL, b.cost_per_task_usd, NULL, 1,
           NULL, b.benchmark_source, NULL, NULL, NULL, b.updated_at, 'aa-index', 1, b.verified_tok_per_s
    FROM catalog_benchmarks b JOIN catalog_models m USING (model_id)
), ranked AS (
    SELECT *, row_number() OVER (
        PARTITION BY model_id, effort, harness
        ORDER BY score IS NULL,
                 CASE origin WHEN 'own' THEN 0 WHEN 'deepswe' THEN 1 WHEN 'cursorbench' THEN 2
                    WHEN 'frontierswe' THEN 3 ELSE 4 END, legacy
    ) AS preference
    FROM observations
)
SELECT model_id, effort, harness, score, score_se, n_runs, steps,
       COALESCE(repriced_cost, cost_per_task_usd) AS cost_per_task_usd,
       tokens_per_task, CASE WHEN origin <> 'own' AND (
           lower(model_id) LIKE '%gpt-6.2-sol%' OR lower(model_id) LIKE '%gpt-6.1-sol%'
           OR lower(model_id) LIKE '%gpt-6.1-terra%' OR lower(model_id) LIKE '%gpt-6-luna%'
           OR lower(model_id) LIKE '%mimo-v2.6%') THEN 1 ELSE imputed END AS imputed,
       effort_channel, index_version, source_url, source_date, source_record,
       measured_at, origin, legacy, verified_tok_per_s,
       COALESCE(julianday(measured_at) < julianday('now', '-45 days'), 1)
           OR stale_reason IS NOT NULL AS stale,
       stale_reason, COALESCE(price_disagreement, 0) AS price_disagreement
FROM ranked LEFT JOIN catalog_benchmark_refresh_flags USING (model_id, effort, harness)
WHERE preference = 1;

CREATE TABLE IF NOT EXISTS catalog_routes (
    route_id TEXT PRIMARY KEY,
    role_name TEXT NOT NULL,
    min_context_tokens INTEGER NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS catalog_route_legs (
    route_id TEXT NOT NULL REFERENCES catalog_routes(route_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    model_id TEXT NOT NULL REFERENCES catalog_models(model_id),
    min_risk_band TEXT NOT NULL DEFAULT 'low',
    PRIMARY KEY (route_id, position)
);

CREATE INDEX IF NOT EXISTS idx_catalog_models_provider ON catalog_models(provider_id);
CREATE INDEX IF NOT EXISTS idx_catalog_models_quota ON catalog_models(quota_pool_id);
CREATE INDEX IF NOT EXISTS idx_catalog_models_family ON catalog_models(family);
CREATE INDEX IF NOT EXISTS idx_catalog_route_legs_model ON catalog_route_legs(model_id);
