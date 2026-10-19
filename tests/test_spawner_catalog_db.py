"""Tests for Plangraph Spawner SQLite Catalog, Models, Routes, and Host Bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from plangraph.spawner import CatalogDB, TIER_RUNG
from plangraph.spawner.catalog_db import SCHEMA_PATH


@pytest.fixture
def memory_db() -> CatalogDB:
    """Provide an initialized in-memory CatalogDB instance."""
    db = CatalogDB(":memory:")
    db.init_and_seed()
    try:
        yield db
    finally:
        db.close()


def test_schema_initialization_and_idempotency(tmp_path: Path) -> None:
    """Schema should initialize and be completely idempotent across successive runs."""
    db_file = tmp_path / "catalog_test.db"
    db = CatalogDB(db_file)
    try:
        # First init and seed
        db.init_and_seed()
        models_first = db.list_models()
        assert len(models_first) == 23
        assert len({m["master_rank"] for m in models_first if m.get("master_rank")}) == 22

        # Second init and seed (must not raise UNIQUE constraint violations)
        db.init_and_seed()
        models_second = db.list_models()
        assert len(models_second) == 23

        # Verify key tables exist with rows
        tables = [
            "catalog_quota_pools",
            "catalog_providers",
            "catalog_clients",
            "catalog_models",
            "catalog_benchmarks",
            "catalog_routes",
            "catalog_route_legs",
        ]
        for tbl in tables:
            count = db.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert count > 0, f"Table {tbl} should not be empty"
    finally:
        db.close()


def test_all_22_models_and_exact_aa_v432_benchmarks(memory_db: CatalogDB) -> None:
    """Verify all 22 models are present with exact AA v4.3.2 matrix figures."""
    models = memory_db.list_models()
    assert len(models) == 23
    assert len({m["master_rank"] for m in models if m.get("master_rank")}) == 22

    # 1. mimo/mimo-v2.6-flash: 54.0 overall verified, 29.0 direct, 75.4 cyberbench, 0.04 USD cost
    flash = memory_db.get_model("mimo/mimo-v2.6-flash")
    assert flash is not None
    assert flash["verified_tok_per_s"] == 54.0
    assert flash["direct_api_tok_per_s"] == 29.0
    assert flash["cyberbench"] == 75.4
    assert flash["cost_per_task_usd"] == 0.04
    assert flash["cache_hit_rate"] == 0.924
    assert flash["aa_intelligence"] == 37.9
    assert flash["benchmark_source"] == "AA-v4.3.2-2026-10"

    # 2. mimo/mimo-v2.6-pro: 46.0 aa_intelligence > opencode-go/qwen3.8-max 45.4
    pro = memory_db.get_model("mimo/mimo-v2.6-pro")
    qwen = memory_db.get_model("opencode-go/qwen3.8-max")
    assert pro is not None
    assert qwen is not None
    assert pro["aa_intelligence"] == 46.0
    assert pro["direct_api_tok_per_s"] == 29.0
    assert pro["cost_per_task_usd"] == 0.13
    assert pro["cache_hit_rate"] == 0.924
    assert qwen["aa_intelligence"] == 45.4
    assert pro["aa_intelligence"] > qwen["aa_intelligence"]

    # 3. agy/claude-opus-5-5-high: rank 1, intelligence 57.6, terminal 60.0, deepswe 74.2
    opus = memory_db.get_model("agy/claude-opus-5-5-high")
    assert opus is not None
    assert opus["master_rank"] == 1
    assert opus["aa_intelligence"] == 57.6
    assert opus["terminal_bench"] == 60.0
    assert opus["deepswe"] == 74.2
    assert opus["verified_tok_per_s"] == 98.0
    assert opus["cost_per_task_usd"] == 1.82

    # 4. gemini/gemini-4-argon-high: rank 2, intelligence 53.0, deepswe 77.9
    argon = memory_db.get_model("gemini/gemini-4-argon-high")
    assert argon is not None
    assert argon["master_rank"] == 2
    assert argon["aa_intelligence"] == 53.0
    assert argon["terminal_bench"] == 57.0
    assert argon["deepswe"] == 77.9
    assert argon["status"] == "upgrade"
    assert argon["available"] == 0

    # 5. cxa/gpt-6-astra-max: rank 3, intelligence 53.0, coding_index 62.0
    astra = memory_db.get_model("cxa/gpt-6-astra-max")
    assert astra is not None
    assert astra["master_rank"] == 3
    assert astra["aa_intelligence"] == 53.0
    assert astra["coding_index"] == 62.0
    assert astra["cost_per_task_usd"] == 3.26

    # 6. agy/claude-sonnet-5-5-xhigh: rank 4, terminal 70.6, frontier_code 52.1
    sonnet_xhigh = memory_db.get_model("agy/claude-sonnet-5-5-xhigh")
    assert sonnet_xhigh is not None
    assert sonnet_xhigh["master_rank"] == 4
    assert sonnet_xhigh["terminal_bench"] == 70.6
    assert sonnet_xhigh["frontier_code"] == 52.1

    # 7. opencode-go/muse-spark-1.3-contributor: coding 75.8, agentic 55.5, tok_s 287.0
    muse = memory_db.get_model("opencode-go/muse-spark-1.3-contributor")
    assert muse is not None
    assert muse["coding_index"] == 75.8
    assert muse["agentic_index"] == 55.5
    assert muse["verified_tok_per_s"] == 287.0

    # 8. opencode-go/glm-5.2-max: coding 71.5, scicode 52.0
    glm = memory_db.get_model("opencode-go/glm-5.2-max")
    assert glm is not None
    assert glm["coding_index"] == 71.5
    assert glm["scicode"] == 52.0


def test_5_rung_tier_ladder_ordering(memory_db: CatalogDB) -> None:
    """Verify trial < free < credit < subscription < paid tier ladder sorting."""
    assert TIER_RUNG == {
        "trial": 0,
        "free": 1,
        "credit": 2,
        "subscription": 3,
        "paid": 4,
    }

    all_models = memory_db.list_models()
    assert len(all_models) == 23

    # Check non-decreasing tier rung sequence
    rungs = [TIER_RUNG[m["tier"]] for m in all_models]
    assert rungs == sorted(rungs)

    # Free models appear first (qoder/qwen3.8-flash-next, agy/gemini-3.8-flash-tiered-high)
    free_models = [m for m in all_models if m["tier"] == "free"]
    assert len(free_models) >= 2
    for m in free_models:
        assert TIER_RUNG[m["tier"]] == 1

    # Subscription models come after free and before paid
    sub_models = [m for m in all_models if m["tier"] == "subscription"]
    assert len(sub_models) > 0

    # Paid models come last
    paid_models = [m for m in all_models if m["tier"] == "paid"]
    assert len(paid_models) >= 2
    for pm in paid_models:
        assert pm["model_id"] in (
            "deepseek/deepseek-v4-flash-max",
            "gemini/gemini-4-argon-high",
        )


def test_codex_acp_luna_only_invariant(memory_db: CatalogDB) -> None:
    """Verify Codex ACP luna-only invariant: cxa luna active, while astra and sol-* are gated."""
    luna = memory_db.get_model("cxa/gpt-6-luna-high")
    assert luna is not None
    assert luna["status"] == "active"
    assert luna["available"] == 1
    assert luna["unavailable_reason"] is None

    gated_cxa_models = [
        "cxa/gpt-6-astra-max",
        "cxa/gpt-6-sol-max",
        "cxa/gpt-6-sol-xhigh",
        "cxa/gpt-6-sol-high",
        "cxa/gpt-6-sol-medium",
        "cxa/gpt-6-sol-low",
    ]

    for model_id in gated_cxa_models:
        m = memory_db.get_model(model_id)
        assert m is not None, f"Model {model_id} must exist"
        assert m["status"] == "gated"
        assert m["available"] == 0
        assert m["unavailable_reason"] == "Codex ACP currently serves luna only"


def test_mimo_pro_precedes_qwen_max_in_routes(memory_db: CatalogDB) -> None:
    """Verify mimo-v2.6-pro precedes qwen3.8-max in l1 and l2, and paid is last in l2."""
    # 1. l1-orchestrator
    l1_legs = memory_db.get_route_legs("l1-orchestrator", active_only=True)
    l1_ids = [leg["model_id"] for leg in l1_legs]
    assert "mimo/mimo-v2.6-pro" in l1_ids
    assert "opencode-go/qwen3.8-max" in l1_ids
    assert l1_ids.index("mimo/mimo-v2.6-pro") < l1_ids.index("opencode-go/qwen3.8-max")

    # 2. l2-worker
    l2_legs = memory_db.get_route_legs("l2-worker", active_only=True)
    l2_ids = [leg["model_id"] for leg in l2_legs]
    assert "mimo/mimo-v2.6-pro" in l2_ids
    assert "opencode-go/qwen3.8-max" in l2_ids
    assert l2_ids.index("mimo/mimo-v2.6-pro") < l2_ids.index("opencode-go/qwen3.8-max")

    # Paid model must be last in l2-worker
    assert l2_ids[-1] == "deepseek/deepseek-v4-flash-max"
    for leg in l2_legs[:-1]:
        assert leg["tier"] in ("free", "trial", "credit", "subscription")

    # 3. cxa/gpt-6-luna-high must be active in l2-worker, l2-reviewer, and l3-driver
    for route_id in ("l2-worker", "l2-reviewer", "l3-driver"):
        route_ids = [leg["model_id"] for leg in memory_db.get_route_legs(route_id, active_only=True)]
        assert (
            "cxa/gpt-6-luna-high" in route_ids
        ), f"cxa/gpt-6-luna-high must be active in {route_id}"


def test_family_and_intelligence_filtering(memory_db: CatalogDB) -> None:
    """Verify family inclusion, exclusion, and intelligence threshold filters."""
    # Family filter
    claude_models = memory_db.list_models(family="claude")
    assert len(claude_models) > 0
    assert all(m["family"] == "claude" for m in claude_models)

    # Exclude families
    no_claude_no_mimo = memory_db.list_models(exclude_families=["claude", "mimo"])
    assert not any(m["family"] in ("claude", "mimo") for m in no_claude_no_mimo)

    # Route legs filtering
    l1_legs_all = memory_db.get_route_legs("l1-orchestrator", active_only=True)
    l1_legs_no_claude = memory_db.get_route_legs(
        "l1-orchestrator", active_only=True, exclude_families=["claude"]
    )
    assert len(l1_legs_no_claude) < len(l1_legs_all)
    assert not any(l["family"] == "claude" for l in l1_legs_no_claude)

    # Intelligence filter
    l1_high_intel = memory_db.get_route_legs(
        "l1-orchestrator", active_only=True, min_intelligence=46.0
    )
    assert all(l["aa_intelligence"] >= 46.0 for l in l1_high_intel)


def test_export_json_and_combos_roundtrip(memory_db: CatalogDB, tmp_path: Path) -> None:
    """Verify deterministic JSON export and roundtrip import into fresh database."""
    export_file = tmp_path / "catalog_export.json"
    combos_file = tmp_path / "combos_export.json"

    # Export catalog tables
    exported_data = memory_db.export_json(export_file)
    assert export_file.is_file()
    assert len(exported_data["models"]) == 23
    assert len(exported_data["benchmarks"]) == 23
    assert len(exported_data["routes"]) == 4

    # Import into fresh DB
    fresh_db = CatalogDB(":memory:")
    fresh_db.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    imported_count = fresh_db.import_json(export_file)
    assert imported_count > 0

    reloaded_models = fresh_db.list_models()
    assert len(reloaded_models) == 23
    assert len({m["master_rank"] for m in reloaded_models if m.get("master_rank")}) == 22

    # Export OmniRoute combos
    combos_data = memory_db.export_combos(combos_file)
    assert combos_file.is_file()
    assert "combos" in combos_data

    combos_map = {c["name"]: c for c in combos_data["combos"]}
    assert "l1-orchestrator" in combos_map
    assert "l2-worker" in combos_map
    assert "l2-reviewer" in combos_map
    assert "l3-driver" in combos_map

    # Check 1M context render for l1-orchestrator and 128k for l2-worker
    assert combos_map["l1-orchestrator"]["context"] == "1M"
    assert combos_map["l2-worker"]["context"] == "128k"
    assert combos_map["l3-driver"]["context"] == "128k"

    fresh_db.close()


def test_deploy_requirements_and_bootstrap_scripts() -> None:
    """Verify deploy/requirements-host.json validity and bootstrap scripts."""
    deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
    req_file = deploy_dir / "requirements-host.json"
    ps1_file = deploy_dir / "bootstrap.ps1"
    sh_file = deploy_dir / "bootstrap.sh"

    assert req_file.is_file()
    assert ps1_file.is_file()
    assert sh_file.is_file()

    with open(req_file, "r", encoding="utf-8") as f:
        req_data = json.load(f)

    core = req_data["autoos_components"]["core"]
    agents = req_data["autoos_components"]["agents"]

    assert "git" in core
    assert "python" in core
    assert "nodejs" in core
    assert "tailscale" in core
    assert "omniroute" in core
    assert "opencode-cli" in core

    assert "claude-code" in agents
    assert "qwen-code" in agents
    assert "gemini-cli" in agents
    assert "qoder-cli" in agents
    assert "agent-skill-links" in agents

    # Validate bootstrap.ps1 syntax via PowerShell dry run
    if sys.platform == "win32":
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1_file),
            "-DryRun",
            "-SkipAutoOS",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, f"bootstrap.ps1 dry-run failed: {proc.stderr}"
        assert "[DRY-RUN]" in proc.stdout
