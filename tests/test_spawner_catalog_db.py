"""Tests for Plangraph Spawner SQLite Catalog, Models, Routes, and Host Bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from plangraph.spawner import CatalogDB, TIER_RUNG
from plangraph.spawner.catalog_db import SCHEMA_PATH


def test_registered_grok_and_console_muse_catalog_derivation():
    # Runtime admission derives new seats from DEFAULT_LEGS, not AA seed rows.
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner.resolver import DEFAULT_FALLBACK_MODELS
    rows = {row["id"]: row for row in DEFAULT_FALLBACK_MODELS}
    legs = {leg.id: leg for leg in DEFAULT_LEGS}
    for lid in ("omniroute-grok-4.7", "opencode-console-muse-spark"):
        leg = legs[lid]
        assert rows[leg.model]["active"]
        assert rows[leg.model]["family"] == leg.family


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
        db.init_and_seed()
        models_first = db.list_models()
        assert len(models_first) == 61  # benchmark-only effort/item identities included
        assert len({m["master_rank"] for m in models_first if m.get("master_rank")}) == 22

        db.init_and_seed()
        models_second = db.list_models()
        assert len(models_second) == 61

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
    """Historical test ID retained; assert current observations, not AA v4.3.2."""
    from functools import partial
    get_observation = partial(memory_db.get_model, metric_view="benchmark")
    models = memory_db.list_models()
    assert len(models) == 61
    assert len({m["master_rank"] for m in models if m.get("master_rank")}) == 22

    flash = get_observation("mimo/mimo-v2.6-flash")
    assert flash is not None
    assert flash["verified_tok_per_s"] == 38.0
    assert flash["direct_api_tok_per_s"] is None
    assert flash["cyberbench"] is None
    assert flash["cost_per_task_usd"] == 0.13
    assert flash["cache_hit_rate"] is None
    assert flash["aa_intelligence"] == 27.0
    assert flash["benchmark_source"] == "AA-v5.0.1 snapshot"

    pro = get_observation("mimo/mimo-v2.6-pro")
    qwen = get_observation("opencode-go/qwen3.8-max")
    assert pro is not None
    assert qwen is not None
    assert pro["aa_intelligence"] == 30.0
    assert pro["direct_api_tok_per_s"] is None
    assert pro["cost_per_task_usd"] == 0.26
    assert pro["cache_hit_rate"] is None
    assert qwen["aa_intelligence"] == 34.0
    assert pro["aa_intelligence"] < qwen["aa_intelligence"]

    opus = get_observation("agy/claude-opus-5-5-high")
    assert opus is not None
    assert opus["master_rank"] == 1
    assert opus["aa_intelligence"] == 52.0
    assert opus["terminal_bench"] is None
    assert opus["deepswe"] is None
    assert opus["verified_tok_per_s"] == 105.0
    assert opus["cost_per_task_usd"] == 3.46

    argon_oc = get_observation("opencode-go/gemini-4-argon")
    assert argon_oc is not None
    assert argon_oc["master_rank"] == 2
    assert argon_oc["aa_intelligence"] == 50.0
    assert argon_oc["deepswe"] is None
    assert argon_oc["status"] == "active"
    assert argon_oc["available"] == 1
    assert argon_oc["tier"] == "subscription"

    argon = get_observation("gemini/gemini-4-argon-high")
    assert argon is not None
    assert argon["master_rank"] == 2
    assert argon["aa_intelligence"] == 50.0
    assert argon["terminal_bench"] is None
    assert argon["deepswe"] is None
    assert argon["status"] == "upgrade"
    assert argon["available"] == 0

    astra = get_observation("cxa/gpt-6-astra-max")
    assert astra is not None
    assert astra["master_rank"] == 3
    assert astra["aa_intelligence"] == 57.0
    assert astra["coding_index"] is None
    assert astra["cost_per_task_usd"] == 6.24

    sonnet_xhigh = get_observation("agy/claude-sonnet-5-5-xhigh")
    assert sonnet_xhigh is not None
    assert sonnet_xhigh["master_rank"] == 4
    assert sonnet_xhigh["terminal_bench"] is None
    assert sonnet_xhigh["frontier_code"] is None

    muse = get_observation("opencode-go/muse-spark-1.3-contributor")
    assert muse is not None
    assert muse["coding_index"] is None and muse["deepswe"] == 75.4
    assert muse["agentic_index"] is None
    assert muse["verified_tok_per_s"] is None

    glm = get_observation("opencode-go/glm-5.2")
    assert glm is not None
    assert glm["coding_index"] is None
    assert glm["scicode"] == pytest.approx(51.1574074074074)
    for row in (flash, pro, qwen, opus, argon_oc, argon, astra, sonnet_xhigh, muse, glm):
        assert row["source_url"].startswith("https://") and row["source_date"] == "2026-11-12"

def test_catalog_leg_without_benchmark_is_refused(tmp_path):
    from plangraph.planner import legs
    from plangraph.planner.routing import BenchRequired, plan_spawn
    from plangraph.spawner.bench_entry import benchmark_options, LEG_BENCHMARK_MODEL
    leg = next(l for l in legs.spawnable_legs() if l.id == "cx-gpt-6.1-sol")
    with CatalogDB(tmp_path / "catalog.db") as db:
        db.init_and_seed()
        db.conn.execute("DELETE FROM catalog_benchmarks WHERE model_id=?", (LEG_BENCHMARK_MODEL[leg.id],))
        db.conn.commit()
    with pytest.raises(BenchRequired, match=leg.id):
        options = benchmark_options(True, str(tmp_path / "catalog.db"))
        plan_spawn({"estimate_tokens": 1000}, role="implement", legs=(leg,), benchmark=options["benchmark"], strict=True)

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
    assert len(all_models) == 61

    # Check non-decreasing tier rung sequence
    rungs = [TIER_RUNG[m["tier"]] for m in all_models]
    assert rungs == sorted(rungs)

    free_models = [m for m in all_models if m["tier"] == "free"]
    assert len(free_models) >= 2
    for m in free_models:
        assert TIER_RUNG[m["tier"]] == 1

    sub_models = [m for m in all_models if m["tier"] == "subscription"]
    assert len(sub_models) > 0

    paid_models = [m for m in all_models if m["tier"] == "paid"]
    assert len(paid_models) >= 2
    for pm in paid_models:
        assert pm["model_id"] in (
            "deepseek/deepseek-v4-flash-max",
            "gemini/gemini-4-argon-high",
            "omniroute/gemini-3.8-flash",
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


def test_sonnet_medium_in_l2_worker_and_reviewer_routes() -> None:
    """Verify init_and_seed on an in-memory CatalogDB places agy/claude-sonnet-5-5-medium and route positions have no gaps."""
    db = CatalogDB(":memory:")
    db.init_and_seed()
    try:
        l2_worker_legs = db.get_route_legs("l2-worker", active_only=True)
        assert len(l2_worker_legs) >= 3
        assert l2_worker_legs[2]["position"] == 3
        assert l2_worker_legs[2]["model_id"] == "agy/claude-sonnet-5-5-medium"

        l2_reviewer_legs = db.get_route_legs("l2-reviewer", active_only=True)
        assert len(l2_reviewer_legs) >= 2
        assert l2_reviewer_legs[1]["position"] == 2
        assert l2_reviewer_legs[1]["model_id"] == "agy/claude-sonnet-5-5-medium"

        routes = [
            r[0]
            for r in db.conn.execute(
                "SELECT route_id FROM catalog_routes ORDER BY route_id ASC"
            ).fetchall()
        ]
        for route_id in routes:
            raw_legs = db.conn.execute(
                "SELECT position FROM catalog_route_legs WHERE route_id = ? ORDER BY position ASC",
                (route_id,),
            ).fetchall()
            raw_positions = [row[0] for row in raw_legs]
            assert raw_positions == list(range(1, len(raw_positions) + 1))

            active_legs = db.get_route_legs(route_id, active_only=True)
            active_positions = [leg["position"] for leg in active_legs]
            assert active_positions == list(range(1, len(active_positions) + 1))
    finally:
        db.close()


def test_family_and_intelligence_filtering(memory_db: CatalogDB) -> None:
    """Verify family inclusion, exclusion, and intelligence threshold filters."""
    claude_models = memory_db.list_models(family="claude")
    assert len(claude_models) > 0
    assert all(m["family"] == "claude" for m in claude_models)

    no_claude_no_mimo = memory_db.list_models(exclude_families=["claude", "mimo"])
    assert not any(m["family"] in ("claude", "mimo") for m in no_claude_no_mimo)

    l1_legs_all = memory_db.get_route_legs("l1-orchestrator", active_only=True)
    l1_legs_no_claude = memory_db.get_route_legs(
        "l1-orchestrator", active_only=True, exclude_families=["claude"]
    )
    assert len(l1_legs_no_claude) < len(l1_legs_all)
    assert not any(l["family"] == "claude" for l in l1_legs_no_claude)

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
    assert len(exported_data["models"]) == 61
    assert len(exported_data["benchmarks"]) == 60
    assert len(exported_data["routes"]) == 4

    # Import into fresh DB
    fresh_db = CatalogDB(":memory:")
    fresh_db.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    imported_count = fresh_db.import_json(export_file)
    assert imported_count > 0

    reloaded_models = fresh_db.list_models()
    assert len(reloaded_models) == 61
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
