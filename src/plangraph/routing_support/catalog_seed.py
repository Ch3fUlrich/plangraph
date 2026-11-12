"""Plangraph Spawner Catalog Seed Data.

Contains the canonical 5-rung tier ladder, provider, client, quota pool, model,
exact-identity benchmark snapshots, upgrades, and role route seed definitions.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

# SEED_MODELS moved to plangraph.catalog_data (neutral module); re-exported
# here so spawner importers keep working unchanged.
from plangraph.catalog_data import SEED_MODELS as SEED_MODELS

# 5-rung tier ladder ordering contract:
# trial (0) < free (1) < credit (2) < subscription (3) < paid (4)
TIER_RUNG: dict[str, int] = {
    "trial": 0,
    "free": 1,
    "credit": 2,
    "subscription": 3,
    "paid": 4,
}

SEED_QUOTA_POOLS: list[dict[str, Any]] = [
    {
        "pool_id": "agy-3p-partner",
        "provider_id": "agy",
        "tier": "subscription",
        "window_type": "sliding_5h",
        "pool_burn_multiplier": 2.0,
        "cache_hit_discount": 0.0,
        "notes": "Anthropic Claude on AgY 3P partner allocation",
    },
    {
        "pool_id": "gemini-api-paid",
        "provider_id": "gemini",
        "tier": "paid",
        "window_type": "monthly_payg",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Google Cloud Vertex / Gemini API direct PAYG",
    },
    {
        "pool_id": "cxa-sub",
        "provider_id": "cxa",
        "tier": "subscription",
        "window_type": "weekly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Codex subscription pool",
    },
    {
        "pool_id": "opencode-go-muse",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go Muse allocation",
    },
    {
        "pool_id": "mimo-sub",
        "provider_id": "mimo",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 0.5,
        "cache_hit_discount": 0.924,
        "notes": "Mimo subscription pool with 0.5x burn and 92.4% cache hit discount",
    },
    {
        "pool_id": "opencode-go-qwen",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go Qwen pool",
    },
    {
        "pool_id": "opencode-go-argon",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go Gemini 4 Argon allocation",
    },
    {
        "pool_id": "opencode-go-deepseek",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go DeepSeek subscription pool",
    },
    {
        "pool_id": "deepseek-payg",
        "provider_id": "deepseek",
        "tier": "paid",
        "window_type": "monthly_payg",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "DeepSeek direct API PAYG paid pool",
    },
    {
        "pool_id": "opencode-go-glm",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go GLM allocation",
    },
    {
        "pool_id": "opencode-go-kimi",
        "provider_id": "opencode-go",
        "tier": "subscription",
        "window_type": "monthly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Opencode Go Kimi pool",
    },
    {
        "pool_id": "claude-oauth",
        "provider_id": "claude",
        "tier": "subscription",
        "window_type": "hourly_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Claude OAuth CLI subscription pool",
    },
    {
        "pool_id": "agy-gemini-flash",
        "provider_id": "agy",
        "tier": "free",
        "window_type": "daily_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "AgY Gemini Flash free tiered allocation",
    },
    {
        "pool_id": "free-qwen-flash",
        "provider_id": "qoder",
        "tier": "free",
        "window_type": "daily_quota",
        "pool_burn_multiplier": 1.0,
        "cache_hit_discount": 0.0,
        "notes": "Qoder free Qwen Flash tier",
    },
]

SEED_PROVIDERS: list[dict[str, Any]] = [
    {"provider_id": "opencode", "omniroute_id": "opencode", "tier": "free", "transport": "gateway", "gateway_mode": "omniroute_acp", "available": 1, "updated_at": "2026-11-12T00:00:00Z"},
    {"provider_id": "omniroute", "omniroute_id": "omniroute", "tier": "subscription", "transport": "gateway", "gateway_mode": "omniroute_acp", "available": 1, "updated_at": "2026-11-12T00:00:00Z"},
    {"provider_id": "gc", "omniroute_id": "gc", "tier": "subscription",
     "transport": "gateway", "gateway_mode": "omniroute_acp", "available": 1,
     "unavailable_reason": None, "updated_at": "2026-11-12T00:00:00Z"},
    {"provider_id": "console-meta", "omniroute_id": "console-meta",
     "tier": "subscription", "transport": "gateway", "gateway_mode": "omniroute_acp",
     "available": 1, "unavailable_reason": None, "updated_at": "2026-11-12T00:00:00Z"},
    {
        "provider_id": "agy",
        "omniroute_id": "agy",
        "tier": "subscription",
        "transport": "acp",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "cxa",
        "omniroute_id": "cxa",
        "tier": "subscription",
        "transport": "acp",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "cx",
        "omniroute_id": "cx",
        "tier": "subscription",
        "transport": "gateway",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-26T00:00:00Z",
    },
    {
        "provider_id": "mimo",
        "omniroute_id": "mimo",
        "tier": "subscription",
        "transport": "gateway",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "opencode-go",
        "omniroute_id": "opencode-go",
        "tier": "subscription",
        "transport": "gateway",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "deepseek",
        "omniroute_id": "deepseek",
        "tier": "paid",
        "transport": "gateway",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "gemini",
        "omniroute_id": "gemini",
        "tier": "paid",
        "transport": "both",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "claude",
        "omniroute_id": "claude",
        "tier": "subscription",
        "transport": "both",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
    {
        "provider_id": "qoder",
        "omniroute_id": "qoder",
        "tier": "free",
        "transport": "gateway",
        "gateway_mode": "omniroute_acp",
        "available": 1,
        "unavailable_reason": None,
        "updated_at": "2026-10-19T00:00:00Z",
    },
]

SEED_CLIENTS: list[dict[str, Any]] = [
    {
        "client_id": "opencode",
        "binary_name": "opencode",
        "transport": "gateway",
        "gateway_first": 1,
        "tier": "subscription",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "agy",
        "binary_name": "agy",
        "transport": "acp",
        "gateway_first": 1,
        "tier": "subscription",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "codex",
        "binary_name": "codex",
        "transport": "acp",
        "gateway_first": 1,
        "tier": "subscription",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "qodercli",
        "binary_name": "qodercli",
        "transport": "cli",
        "gateway_first": 1,
        "tier": "free",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "claude",
        "binary_name": "claude",
        "transport": "cli",
        "gateway_first": 1,
        "tier": "subscription",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "gemini",
        "binary_name": "gemini",
        "transport": "cli",
        "gateway_first": 1,
        "tier": "subscription",
        "available": 1,
        "unavailable_reason": None,
    },
    {
        "client_id": "qwen",
        "binary_name": "qwen",
        "transport": "cli",
        "gateway_first": 1,
        "tier": "free",
        "available": 1,
        "unavailable_reason": None,
    },
]


def snapshot_data():
    """Frozen upstream observations and historical upgrade fingerprints."""
    return json.loads(Path(__file__).with_name("catalog_benchmark_sources.json").read_text(encoding="utf-8"))


def published_rows():
    """Only current, exact-identity observations enter the benchmark seed."""
    rows = json.loads(Path(__file__).with_name("catalog_benchmarks.json").read_text(encoding="utf-8"))
    if len({row["model_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate benchmark model/effort")
    return rows


def migrate(conn):
    """Add nullable provenance/components and preserve every operator value."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(catalog_benchmarks)")}
    for column, kind in (("source_url", "TEXT"), ("source_date", "TEXT"),
                         ("swe_bench_verified", "REAL"), ("swe_atlas_qna", "REAL")):
        if column not in columns:
            conn.execute(f"ALTER TABLE catalog_benchmarks ADD COLUMN {column} {kind}")
    model_columns = {r[1] for r in conn.execute("PRAGMA table_info(catalog_models)")}
    if "benchmark_only" not in model_columns:
        conn.execute("ALTER TABLE catalog_models ADD COLUMN benchmark_only INTEGER NOT NULL DEFAULT 0")
    info = list(conn.execute("PRAGMA table_info(catalog_benchmarks)"))
    nullable = {"aa_intelligence", "verified_tok_per_s", "cost_per_task_usd"}
    if any(r[3] and r[1] in nullable for r in info):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='catalog_benchmarks'").fetchone()[0]
        for column in nullable:
            sql = sql.replace(column + " REAL NOT NULL", column + " REAL")
        conn.execute("ALTER TABLE catalog_benchmarks RENAME TO catalog_benchmarks_legacy")
        conn.execute(sql)
        names = ", ".join('"' + r[1] + '"' for r in info)
        conn.execute(f"INSERT INTO catalog_benchmarks ({names}) SELECT {names} FROM catalog_benchmarks_legacy")
        conn.execute("DROP TABLE catalog_benchmarks_legacy")


def backfill_provenance(conn, seeds):
    """Refresh exact old seeds (including PR self-citations), never operator edits."""
    conn.execute("UPDATE catalog_models SET upstream_id='muse-spark-1.3-contributor',parameter_effort='max' "
                 "WHERE model_id='opencode-go/muse-spark-1.3-contributor' "
                 "AND upstream_id='muse-spark-1.3' AND parameter_effort='xhigh'")
    snapshot = snapshot_data()
    previous = {model: evidence["metrics"] for model, evidence in snapshot["legacy"].items()}
    for seed in seeds:
        raw = conn.execute("SELECT * FROM catalog_benchmarks WHERE model_id=?", (seed["model_id"],)).fetchone()
        if raw is None:
            continue
        row = dict(raw)
        legacy = previous.get(seed["model_id"])
        # Fingerprints retain upgrade recognition without keeping misleading URLs
        # in a second set of source-looking records. Extra operator fields change it.
        normalized = {key: float(value) if type(value) in (int, float) else value
                      for key, value in row.items() if value is not None}
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        legacy_provenance = (row["source_url"] is None and row["source_date"] is None
                             or "/git/blobs/" in (row["source_url"] or ""))
        if (legacy and legacy_provenance and all(row[key] == value for key, value in legacy.items())
                and all(value is None for key, value in row.items()
                        if key not in legacy and key not in {"source_url", "source_date"})):
            fields = {key: seed.get(key) for key in row if key != "model_id"}
        elif fingerprint == snapshot["prior_pr"].get(seed["model_id"]):
            fields = {key: seed.get(key) for key in row if key != "model_id"}
        elif row["source_url"] is None and row["source_date"] is None and all(
                row[key] == value for key, value in seed.items() if key not in {"source_url", "source_date"}):
            fields = {key: seed[key] for key in ("source_url", "source_date")}
        else:
            continue
        assignments = ", ".join(key + "=?" for key in fields)
        conn.execute(f"UPDATE catalog_benchmarks SET {assignments} WHERE model_id=?",
                     (*fields.values(), seed["model_id"]))


SEED_BENCHMARKS = published_rows()
# The 4 Role Routes
SEED_ROUTES: list[dict[str, Any]] = [
    {
        "route_id": "l1-orchestrator",
        "role_name": "L1 Orchestrator",
        "min_context_tokens": 600000,
        "description": "High intelligence orchestration and planning with large context (>=600k)",
    },
    {
        "route_id": "l2-worker",
        "role_name": "L2 Worker",
        "min_context_tokens": 128000,
        "description": "General implementation worker (128k clamp)",
    },
    {
        "route_id": "l2-reviewer",
        "role_name": "L2 Reviewer",
        "min_context_tokens": 128000,
        "description": "Independent code review and QA verification",
    },
    {
        "route_id": "l3-driver",
        "role_name": "L3 Driver",
        "min_context_tokens": 128000,
        "description": "High-speed test driving and execution loop",
    },
]

# Canonical Route Legs
SEED_ROUTE_LEGS: list[dict[str, Any]] = [
    # l1-orchestrator legs (>=600k):
    # mimo/mimo-v2.6-pro precedes opencode-go/qwen3.8-max
    {
        "route_id": "l1-orchestrator",
        "position": 1,
        "model_id": "agy/gemini-3.8-flash-tiered-high",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 2,
        "model_id": "agy/claude-opus-5-5-high",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 3,
        "model_id": "opencode-go/gemini-4-argon",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 4,
        "model_id": "agy/claude-sonnet-5-5-xhigh",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 5,
        "model_id": "agy/claude-sonnet-5-5-high",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 6,
        "model_id": "mimo/mimo-v2.6-pro",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 7,
        "model_id": "opencode-go/qwen3.8-max",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 8,
        "model_id": "agy/claude-sonnet-5-5-medium",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 9,
        "model_id": "cxa/gpt-6-luna-high",
        "min_risk_band": "low",
    },
    {
        "route_id": "l1-orchestrator",
        "position": 10,
        "model_id": "mimo/mimo-v2.6-flash",
        "min_risk_band": "low",
    },
    # l2-worker legs:
    # Free legs, then subscription legs (mimo-v2.6-pro precedes qwen3.8-max, cxa luna active),
    # then paid legs last (deepseek/deepseek-v4-flash-max LAST)
    {"route_id": "l2-worker", "position": 1, "model_id": "qoder/qwen3.8-flash-next", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 2, "model_id": "agy/gemini-3.8-flash-tiered-high", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 3, "model_id": "agy/claude-sonnet-5-5-medium", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 4, "model_id": "mimo/mimo-v2.6-pro", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 5, "model_id": "agy/claude-sonnet-5-5-high", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 6, "model_id": "opencode-go/gemini-4-argon", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 7, "model_id": "opencode-go/qwen3.8-max", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 8, "model_id": "opencode-go/deepseek-v4.1-flash", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 9, "model_id": "opencode-go/glm-5.2", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 10, "model_id": "opencode-go/muse-spark-1.3-contributor", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 11, "model_id": "claude/claude-haiku-4-5", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 12, "model_id": "cxa/gpt-6-luna-high", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 13, "model_id": "mimo/mimo-v2.6-flash", "min_risk_band": "low"},
    {"route_id": "l2-worker", "position": 14, "model_id": "deepseek/deepseek-v4-flash-max", "min_risk_band": "low"},
    # l2-reviewer legs:
    # cxa/gpt-6-luna-high active, high code/review fidelity
    {"route_id": "l2-reviewer", "position": 1, "model_id": "agy/claude-sonnet-5-5-xhigh", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 2, "model_id": "agy/claude-sonnet-5-5-medium", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 3, "model_id": "mimo/mimo-v2.6-pro", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 4, "model_id": "opencode-go/gemini-4-argon", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 5, "model_id": "agy/claude-sonnet-5-5-high", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 6, "model_id": "cxa/gpt-6-luna-high", "min_risk_band": "low"},
    {"route_id": "l2-reviewer", "position": 7, "model_id": "opencode-go/glm-5.2", "min_risk_band": "low"},
    # l3-driver legs:
    # cxa/gpt-6-luna-high active, high token speed, paid deepseek last
    {
        "route_id": "l3-driver",
        "position": 1,
        "model_id": "qoder/qwen3.8-flash-next",
        "min_risk_band": "low",
    },
    {
        "route_id": "l3-driver",
        "position": 2,
        "model_id": "cxa/gpt-6-luna-high",
        "min_risk_band": "low",
    },
    {
        "route_id": "l3-driver",
        "position": 3,
        "model_id": "opencode-go/deepseek-v4.1-flash",
        "min_risk_band": "low",
    },
    {
        "route_id": "l3-driver",
        "position": 4,
        "model_id": "mimo/mimo-v2.6-flash",
        "min_risk_band": "low",
    },
    {
        "route_id": "l3-driver",
        "position": 5,
        "model_id": "deepseek/deepseek-v4-flash-max",
        "min_risk_band": "low",
    },
]
