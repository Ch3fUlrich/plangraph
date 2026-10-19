"""Comprehensive pytest suite for Plangraph ACP Spawner, Resolver, and MCP Server.

Validates:
  1. measure.py & risk.py: S0..S4 bucketing, decomposition trigger on S4, high-risk escalation.
  2. clients.py: OmniRoute ACP-first routing, Tailnet/Windows environment preservation, header envelopes.
  3. resolver.py: 5-rung tier ladder, utility scoring, Codex luna-only, Sonnet/Haiku guardrails, cross-family reviews.
  4. runner.py & mcp.py: async job pool, served telemetry extraction, JSON-RPC 2.0 tool dispatch.
"""
from __future__ import annotations

import asyncio
import os
import pytest

from plangraph.spawner.clients import (
    CLIENTS,
    build_child_env,
    build_command,
    build_opencode_command,
    build_routing_envelope_headers,
    build_standalone_fallback_command,
    detect_client_for_model,
)
from plangraph.spawner.mcp import MCPSpawnerServer, TOOLS_METADATA
from plangraph.spawner.measure import (
    classify_bucket,
    estimate_tokens,
    is_doc_file,
    is_docs_only,
    measure,
)
from plangraph.spawner.resolver import (
    TIER_RUNG,
    clamp_effort,
    detect_model_family,
    get_catalog_models,
    is_model_available,
    resolve_review_plan,
    score_model,
)
from plangraph.spawner.risk import (
    RISK_MIN_INTELLIGENCE,
    RISK_REVIEWER_COUNT,
    evaluate_risk,
    is_critical_path,
    is_high_risk_path,
)
from plangraph.spawner.runner import (
    JobInfo,
    SpawnerRunner,
    extract_served_telemetry,
)


# ==============================================================================
# 1. MEASURE & RISK TESTS
# ==============================================================================

def test_measure_bucketing_s0_to_s4():
    """Verify S0..S4 complexity thresholds and decomposition triggers."""
    # S0: trivial change
    b0, dec0 = classify_bucket(file_count=1, line_count=15, fanout=0)
    assert b0 == "S0"
    assert dec0 is False

    # S0: docs-only change
    b_doc, dec_doc = classify_bucket(file_count=3, line_count=200, fanout=0, docs_only=True)
    assert b_doc == "S0"
    assert dec_doc is False

    # S1: small single-module change (<= 2 files, <= 80 lines, fanout <= 2)
    b1, dec1 = classify_bucket(file_count=2, line_count=60, fanout=2)
    assert b1 == "S1"
    assert dec1 is False

    # S2: standard implementation (<= 5 files, <= 250 lines, fanout <= 5)
    b2, dec2 = classify_bucket(file_count=4, line_count=220, fanout=4)
    assert b2 == "S2"
    assert dec2 is False

    # S3: multi-module feature/refactor (<= 12 files or fanout <= 12)
    b3, dec3 = classify_bucket(file_count=8, line_count=500, fanout=8)
    assert b3 == "S3"
    assert dec3 is False

    # S4: large cross-cutting (> 12 files or fanout > 12) -> triggers should_decompose = True
    b4_files, dec4_files = classify_bucket(file_count=14, line_count=100, fanout=1)
    assert b4_files == "S4"
    assert dec4_files is True

    b4_fanout, dec4_fanout = classify_bucket(file_count=3, line_count=50, fanout=16)
    assert b4_fanout == "S4"
    assert dec4_fanout is True


def test_measure_telemetry_object():
    """Test full measurement telemetry structure with declared values."""
    m = measure(
        paths=["src/plangraph/spawner/measure.py", "tests/test_measure.py"],
        lines=75,
        fanout=1,
        has_tests=True,
    )
    assert m.file_count == 2
    assert m.lines == 75
    assert m.fanout == 1
    assert m.has_tests is True
    assert m.bucket == "S1"
    assert m.should_decompose is False
    assert m.need_tokens > 0


def test_risk_evaluation_and_thresholds():
    """Verify 4 risk bands, intelligence thresholds, and reviewer counts."""
    # Low risk (docs or pure S0)
    low_res = evaluate_risk(
        paths=["docs/architecture.md"],
        bucket="S0",
        has_tests=True,
        is_docs_only=True,
        is_pure_doc=True,
    )
    assert low_res.risk == "low"
    assert low_res.min_intelligence == RISK_MIN_INTELLIGENCE["low"]
    assert low_res.reviewer_count == 0  # 0 for pure doc S0

    # Normal risk: standard code change with tests
    norm_res = evaluate_risk(
        paths=["src/plangraph/spawner/helper.py"],
        bucket="S2",
        has_tests=True,
    )
    assert norm_res.risk == "normal"
    assert norm_res.min_intelligence == 44.0
    assert norm_res.reviewer_count == 1

    # High risk: touches setup.ps1 or setup.sh
    for script_name in ["setup.ps1", "setup.sh", "systemd/plangraph.service", "migrations/001_init.sql"]:
        hr_res = evaluate_risk(paths=[script_name], bucket="S1", has_tests=True)
        assert hr_res.risk == "high"
        assert hr_res.min_intelligence == 46.0
        assert hr_res.reviewer_count == 2

    # High risk: missing tests on S2/S3
    no_test_res = evaluate_risk(
        paths=["src/plangraph/spawner/logic.py"],
        bucket="S2",
        has_tests=False,
    )
    assert no_test_res.risk == "high"

    # Critical risk: touches critical paths (auth, crypto, security, policy.py)
    for crit_file in ["tools/policy.py", "src/auth/token.py", "lib/crypto/cipher.py", "src/security.py"]:
        crit_res = evaluate_risk(paths=[crit_file], bucket="S1", has_tests=True)
        assert crit_res.risk == "critical"
        assert crit_res.min_intelligence == 50.0
        assert crit_res.reviewer_count == 2


# ==============================================================================
# 2. CLIENTS & OMNIROUTE ACP-FIRST TESTS
# ==============================================================================

def test_clients_acp_first_command_building():
    """Verify OmniRoute ACP-first is used by default for all model providers."""
    prompt = "Implement feature X"
    providers = [
        "agy/claude-sonnet-5-5-high",
        "cxa/gpt-6-luna-high",
        "qoder/qwen3.8-flash-next",
        "mimo/mimo-v2.6-pro",
        "opencode-go/qwen3.8-max",
        "claude/claude-sonnet-5-5",
    ]

    for model in providers:
        cmd = build_command(
            prompt=prompt,
            model_or_combo=model,
            gateway=True,
            gateway_online=True,
        )
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--model" in cmd
        assert model in cmd
        assert prompt in cmd


def test_clients_offline_emergency_fallback():
    """Verify direct standalone CLI spawning occurs ONLY when gateway_online=False."""
    prompt = "Emergency task"

    # Agy standalone fallback
    cmd_agy = build_command(prompt=prompt, model_or_combo="agy/claude-sonnet-5-5-high", gateway_online=False)
    assert cmd_agy[0] == "agy"
    assert "--dangerously-skip-permissions" in cmd_agy
    assert "-p" in cmd_agy

    # Qoder standalone fallback
    cmd_qoder = build_command(prompt=prompt, model_or_combo="qoder/qwen3.8-flash-next", gateway_online=False)
    assert cmd_qoder[0] == "qodercli"
    assert "-p" in cmd_qoder

    # Codex standalone fallback
    cmd_codex = build_command(prompt=prompt, model_or_combo="cxa/gpt-6-luna-high", gateway_online=False)
    assert cmd_codex[0] == "codex"
    assert cmd_codex[1] == "exec"

    # Claude standalone fallback
    cmd_claude = build_command(prompt=prompt, model_or_combo="claude/claude-sonnet-5-5", gateway_online=False)
    assert cmd_claude[0] == "claude"
    assert "-p" in cmd_claude


def test_windows_tailnet_environment_preservation():
    """Verify SystemRoot, COMSPEC, PATHEXT, and TEMP are always preserved on Windows (D-2090 BUG-B)."""
    child_env = build_child_env({"CUSTOM_VAR": "TEST_VAL"})
    assert child_env["CUSTOM_VAR"] == "TEST_VAL"

    if os.name == "nt":
        # Windows must retain these critical system keys
        for key in ("SystemRoot", "COMSPEC", "PATHEXT", "TEMP"):
            if key in os.environ:
                assert key in child_env
                assert child_env[key] == os.environ[key]


def test_routing_envelope_headers():
    """Verify structured routing envelope generates correct headers."""
    # Without exclusions
    h1 = build_routing_envelope_headers(
        role="implement",
        risk="normal",
        min_intelligence=44.0,
        required_context=32000,
        effort="medium",
    )
    assert h1["X-OmniRoute-Task-Role"] == "implement"
    assert h1["X-OmniRoute-Risk"] == "normal"
    assert h1["X-OmniRoute-Min-Intelligence"] == "44.0"
    assert h1["X-OmniRoute-Required-Context"] == "32000"
    assert h1["X-OmniRoute-Effort"] == "medium"
    assert "X-OmniRoute-Exclude-Family" not in h1

    # With family exclusion
    h2 = build_routing_envelope_headers(
        role="review",
        risk="high",
        min_intelligence=46.0,
        required_context=64000,
        exclude_families=["claude", "gpt"],
        effort="high",
    )
    assert h2["X-OmniRoute-Task-Role"] == "review"
    assert h2["X-OmniRoute-Exclude-Family"] == "claude,gpt"

    import json as _json
    env_with_h2 = build_child_env(headers=h2)
    assert "OPENCODE_CONFIG_CONTENT" in env_with_h2
    oc_cfg = _json.loads(env_with_h2["OPENCODE_CONFIG_CONTENT"])
    omni_prov = oc_cfg["providers"]["omniroute"]
    assert omni_prov["settings"]["baseURL"] == "http://127.0.0.1:20128/v1"
    assert omni_prov["headers"]["X-OmniRoute-Task-Role"] == "review"
    assert omni_prov["headers"]["X-OmniRoute-Exclude-Family"] == "claude,gpt"


# ==============================================================================
# 3. RESOLVER & CROSS-FAMILY REVIEW TESTS
# ==============================================================================

def test_resolver_tier_ladder_priority():
    """Verify 5-rung tier ladder strictly prioritizes subscription over paid."""
    assert TIER_RUNG["trial"] == 0
    assert TIER_RUNG["free"] == 1
    assert TIER_RUNG["credit"] == 2
    assert TIER_RUNG["subscription"] == 3
    assert TIER_RUNG["paid"] == 4

    # Identical models differing only in tier
    sub_model = {
        "id": "test/model-sub",
        "aa_intelligence": 45.0,
        "verified_tok_per_s": 60.0,
        "cost_per_task_usd": 0.20,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
    }
    paid_model = dict(sub_model, id="test/model-paid", tier="paid")

    score_sub = score_model(sub_model)
    score_paid = score_model(paid_model)
    assert score_sub > score_paid


def test_resolver_mimo_scores_above_qwen():
    """Verify mimo/mimo-v2.6-pro scores higher than opencode-go/qwen3.8-max for l1 and l2 roles."""
    models = {m["id"]: m for m in get_catalog_models()}
    mimo = models["mimo/mimo-v2.6-pro"]
    qwen = models["opencode-go/qwen3.8-max"]

    for role in ("l1-orchestrator", "l2-worker"):
        score_mimo = score_model(mimo, role=role)
        score_qwen = score_model(qwen, role=role)
        assert score_mimo > score_qwen, f"Expected mimo > qwen for role {role}, got {score_mimo} <= {score_qwen}"


def test_resolver_gemini_argon_smart_model_and_cost_adaptation():
    """Verify opencode-go/gemini-4-argon-contributor is selected as frontier smart model and cost adapts by risk."""
    models = {m["id"]: m for m in get_catalog_models()}
    argon = models["opencode-go/gemini-4-argon-contributor"]
    assert argon is not None
    assert argon["available"] == 1
    assert argon["tier"] == "subscription"
    assert argon["family"] == "gemini"
    assert argon["aa_intelligence"] == 53.0

    # For high risk, argon's 53.0 intelligence and subscription tier beat 46.8 Sonnet
    sonnet = models["agy/claude-sonnet-5-5-high"]
    score_argon_high = score_model(argon, role="implement", risk="high")
    score_sonnet_high = score_model(sonnet, role="implement", risk="high")
    assert score_argon_high > score_sonnet_high

    # Cost adaptation test: for low risk, cheap/free models ($0.00-$0.07, 180-300 tok/s) beat expensive paid models
    qwen_flash = models["qoder/qwen3.8-flash-next"]
    score_qwen_flash_low = score_model(qwen_flash, role="implement", risk="low")
    score_sonnet_low = score_model(sonnet, role="implement", risk="low")
    assert score_qwen_flash_low > score_sonnet_low


def test_resolver_codex_luna_only():
    """Verify Codex ACP allows luna-high while rejecting astra-max and sol-* from SQLite CatalogDB."""
    all_rows = get_catalog_models()
    assert len(all_rows) == 24, "get_catalog_models() must query the full 24-row SQLite CatalogDB"
    models = {m["id"]: m for m in all_rows}

    assert is_model_available(models["cxa/gpt-6-luna-high"]) is True
    assert is_model_available(models["cxa/gpt-6-astra-max"]) is False
    assert is_model_available(models["cxa/gpt-6-sol-max"]) is False
    assert is_model_available(models["cxa/gpt-6-sol-xhigh"]) is False
    assert is_model_available(models["cxa/gpt-6-sol-high"]) is False
    assert is_model_available(models["cxa/gpt-6-sol-medium"]) is False
    assert is_model_available(models["cxa/gpt-6-sol-low"]) is False
    assert is_model_available({"id": "cxa/gpt-6-sol-preview", "available": 1}) is False


def test_resolver_guardrails_effort_and_price_cliff():
    """Verify Sonnet 5.5 max effort clamping and Haiku 100k price cliff."""
    # Sonnet 5.5 max -> xhigh
    assert clamp_effort("claude-sonnet-5-5", "max") == "xhigh"
    assert clamp_effort("agy/claude-sonnet-5-5-high", "max") == "xhigh"
    assert clamp_effort("mimo/mimo-v2.6-pro", "max") == "max"
    assert clamp_effort("claude-sonnet-5-5", "high") == "high"

    # Haiku price cliff at 100k
    plan_under = resolve_review_plan(
        writer_model="mimo/mimo-v2.6-pro",
        risk="normal",
        combo_name="l2-worker",
        need_tokens=50000,
    )
    # Haiku can participate under 100k
    rev_models_under = [r["model"] for r in plan_under["reviewers"]]

    plan_over = resolve_review_plan(
        writer_model="mimo/mimo-v2.6-pro",
        risk="normal",
        combo_name="l2-worker",
        need_tokens=120000,
    )
    rev_models_over = [r["model"] for r in plan_over["reviewers"]]
    assert "claude/claude-haiku-4-5" not in rev_models_over


@pytest.mark.parametrize("writer_model", [
    "agy/claude-sonnet-5-5-high",
    "mimo/mimo-v2.6-pro",
    "opencode-go/muse-spark-1.3-contributor",
    "cxa/gpt-6-luna-high",
])
def test_resolver_cross_family_review_all_writers(writer_model: str):
    """Verify cross-family review plan for high/critical risk enforces 3 distinct families."""
    plan = resolve_review_plan(
        writer_model=writer_model,
        risk="critical",
        combo_name="l2-worker",
        need_tokens=32000,
    )

    writer_family = plan["writer_family"]
    reviewers = plan["reviewers"]
    assert len(reviewers) == 2

    rev1_family = reviewers[0]["family"]
    rev2_family = reviewers[1]["family"]

    # All three families must be strictly distinct
    assert writer_family != rev1_family
    assert writer_family != rev2_family
    assert rev1_family != rev2_family

    # Verify X-OmniRoute-Exclude-Family headers
    h1 = reviewers[0]["headers"]
    assert h1["X-OmniRoute-Exclude-Family"] == writer_family

    h2 = reviewers[1]["headers"]
    assert writer_family in h2["X-OmniRoute-Exclude-Family"]
    assert rev1_family in h2["X-OmniRoute-Exclude-Family"]


# ==============================================================================
# 4. RUNNER & MCP SERVER TESTS
# ==============================================================================

def test_runner_dry_run_spawn_and_status():
    """Verify SpawnerRunner spawns and tracks dry-run jobs."""
    async def _run():
        runner = SpawnerRunner()
        job = await runner.spawn(
            prompt="Dry run test task",
            model_or_combo="mimo/mimo-v2.6-pro",
            role="implement",
            risk="normal",
            dry_run=True,
        )
        assert job.status == "dry_run"
        assert job.served_model == "mimo/mimo-v2.6-pro"
        assert job.served_family == "mimo"

        # Status check
        status = await runner.status(job.job_id)
        assert status.job_id == job.job_id

        # Steer check
        steer_res = await runner.steer(job.job_id, "Keep it concise")
        assert steer_res["steered"] is True
        assert "Keep it concise" in runner.jobs[job.job_id].steer_messages

    asyncio.run(_run())


def test_runner_served_telemetry_extraction():
    """Verify extraction of served_model and served_family from response output."""
    output = """
    Connecting to OmniRoute Gateway...
    X-OmniRoute-Selected-Model: mimo/mimo-v2.6-pro
    X-OmniRoute-Selected-Family: mimo
    Agent execution started...
    """
    model, family = extract_served_telemetry(output)
    assert model == "mimo/mimo-v2.6-pro"
    assert family == "mimo"


def test_mcp_tools_and_rpc_dispatch():
    """Verify MCP tools dispatch and handle JSON-RPC 2.0 requests."""
    async def _run():
        server = MCPSpawnerServer()

        # 1. Initialize
        init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        init_resp = await server.handle_rpc_request(init_req)
        assert init_resp["result"]["serverInfo"]["name"] == "plangraph-spawner"

        # 2. List tools
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        list_resp = await server.handle_rpc_request(list_req)
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        assert "plangraph_agent_spawn" in tool_names
        assert "plangraph_agent_status" in tool_names
        assert "plangraph_agent_steer" in tool_names
        assert "plangraph_agent_cancel" in tool_names
        assert "plangraph_agent_query_catalog" in tool_names
        assert "plangraph_agent_health" in tool_names

        # 3. Query catalog tool call
        cat_call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "plangraph_agent_query_catalog",
                "arguments": {"combo": "l2-worker", "family": "mimo"},
            },
        }
        cat_resp = await server.handle_rpc_request(cat_call)
        assert cat_resp["result"]["isError"] is False
        content_text = cat_resp["result"]["content"][0]["text"]
        assert "mimo/mimo-v2.6-pro" in content_text

        # 4. Dry-run spawn tool call
        spawn_call = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "plangraph_agent_spawn",
                "arguments": {
                    "prompt": "Test agent via MCP",
                    "model_or_combo": "l2-worker",
                    "dry_run": True,
                },
            },
        }
        spawn_resp = await server.handle_rpc_request(spawn_call)
        assert spawn_resp["result"]["isError"] is False
        assert "dry_run" in spawn_resp["result"]["content"][0]["text"]

        # 5. Health tool call
        health_call = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "plangraph_agent_health", "arguments": {}},
        }
        health_resp = await server.handle_rpc_request(health_call)
        assert "healthy" in health_resp["result"]["content"][0]["text"]

    asyncio.run(_run())
