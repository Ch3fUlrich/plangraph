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
    trains_on_prompts INTEGER NOT NULL DEFAULT 0
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
