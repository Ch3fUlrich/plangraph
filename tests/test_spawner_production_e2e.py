"""Production-Like Environment E2E Tests for Plangraph Spawner & OmniRoute Gateway.

Spins up a real local HTTP server (ThreadingHTTPServer on 127.0.0.1:0 ephemeral port)
simulating the live OmniRoute ACP/Gateway in a production-like environment:
  - Exposes /v1/chat/completions and /v1/models
  - Parses incoming HTTP envelope headers (X-OmniRoute-Task-Role, X-OmniRoute-Risk,
    X-OmniRoute-Min-Intelligence, X-OmniRoute-Required-Context, X-OmniRoute-Exclude-Family,
    X-OmniRoute-Effort, X-OmniRoute-Author-Model)
  - Selects route legs from CatalogDB.export_combos() applying production filtering rules:
      * Drops unavailable/gated models (cxa/gpt-6-astra-max, cxa/gpt-6-sol-*)
      * Drops excluded families (X-OmniRoute-Exclude-Family + Author Model family)
      * Enforces X-OmniRoute-Min-Intelligence and price_cliff_tokens (Haiku > 100K)
      * Clamps Sonnet 5.5 max effort to xhigh
      * Returns HTTP 200 with X-OmniRoute-Selected-Model, Selected-Provider, Selected-Family headers
  - Tests end-to-end production workflows:
      1. S2 Normal-Risk implementation + cross-family single review
      2. S3 High-Risk implementation + dual cross-family review (writer != rev1 != rev2)
      3. Vendor guardrails (gated models, price cliffs, effort clamping)
      4. MCP Server (MCPSpawnerServer) production workflow with child process telemetry extraction
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

import pytest

from plangraph.spawner import CatalogDB, TIER_RUNG
from plangraph.spawner.clients import build_child_env, build_routing_envelope_headers
from plangraph.spawner.mcp import MCPSpawnerServer
from plangraph.spawner.measure import measure
from plangraph.spawner.resolver import (
    clamp_effort,
    detect_model_family,
    get_catalog_models,
    is_model_available,
    resolve_model_for_task,
    resolve_review_plan,
    score_model,
)
from plangraph.spawner.risk import evaluate_risk
from plangraph.spawner.runner import JobInfo, SpawnerRunner, extract_served_telemetry


# ==============================================================================
# LIVE OMNIROUTE HTTP GATEWAY SERVER
# ==============================================================================

class LiveOmniRouteHandler(http.server.BaseHTTPRequestHandler):
    """HTTP Request Handler simulating the live OmniRoute ACP Gateway."""

    db: CatalogDB
    combos_data: dict[str, list[str]]

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress standard HTTP server stderr logging in pytest output."""
        return

    def do_GET(self) -> None:
        """Handle /v1/models endpoint."""
        if self.path in ("/v1/models", "/v1/models/"):
            active_models = self.db.list_models(active_only=True)
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": m["model_id"],
                        "object": "model",
                        "owned_by": m.get("provider_id", "omniroute"),
                        "permission": [],
                        "root": m["model_id"],
                        "parent": None,
                    }
                    for m in active_models
                ],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, f"Endpoint not found: {self.path}")

    def do_POST(self) -> None:
        """Handle /v1/chat/completions endpoint with full routing envelope evaluation."""
        if not self.path.startswith("/v1/chat/completions"):
            self.send_error(404, f"Endpoint not found: {self.path}")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            req_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            req_data = {}

        # 1. Parse incoming routing envelope headers
        task_role = self.headers.get("X-OmniRoute-Task-Role", "implement")
        risk_band = self.headers.get("X-OmniRoute-Risk", "normal")
        min_intel_hdr = self.headers.get("X-OmniRoute-Min-Intelligence")
        min_intel = float(min_intel_hdr) if min_intel_hdr else None
        req_context = int(self.headers.get("X-OmniRoute-Required-Context", "32000"))
        exclude_family_hdr = self.headers.get("X-OmniRoute-Exclude-Family", "")
        effort_hdr = self.headers.get("X-OmniRoute-Effort", "medium")
        author_model_hdr = self.headers.get("X-OmniRoute-Author-Model", "")

        # Also support envelope headers injected via OPENCODE_CONFIG_CONTENT env header if present
        cfg_content = self.headers.get("X-Opencode-Config-Content")
        if cfg_content:
            try:
                cfg_obj = json.loads(cfg_content)
                omni_hdrs = cfg_obj.get("providers", {}).get("omniroute", {}).get("headers", {})
                if not exclude_family_hdr and "X-OmniRoute-Exclude-Family" in omni_hdrs:
                    exclude_family_hdr = omni_hdrs["X-OmniRoute-Exclude-Family"]
                if not author_model_hdr and "X-OmniRoute-Author-Model" in omni_hdrs:
                    author_model_hdr = omni_hdrs["X-OmniRoute-Author-Model"]
            except Exception:
                pass

        # 2. Build excluded families set
        excluded_families: set[str] = set()
        if exclude_family_hdr:
            excluded_families.update(
                f.strip().lower() for f in exclude_family_hdr.split(",") if f.strip()
            )
        if author_model_hdr:
            excluded_families.add(detect_model_family(author_model_hdr))

        requested_model = req_data.get("model", "l2-worker")

        # 3. Model candidate selection
        selected_model_dict: dict[str, Any] | None = None

        if requested_model in self.combos_data:
            # Combo route: iterate legs in priority order
            candidate_ids = self.combos_data[requested_model]
            for mid in candidate_ids:
                m = self.db.get_model(mid)
                if not m:
                    continue

                # Filter A: Availability & Gating
                if int(m.get("available", 1)) == 0 or m.get("status") in ("gated", "retired"):
                    continue
                if mid.lower().startswith("cxa/") and not mid.lower().startswith("cxa/gpt-6-luna"):
                    continue

                # Filter B: Excluded families
                fam = detect_model_family(mid)
                if fam in excluded_families:
                    continue

                # Filter C: Minimum intelligence
                if min_intel is not None and float(m.get("aa_intelligence", 0.0)) < min_intel:
                    continue

                # Filter D: Context & Price Cliff
                cliff = m.get("price_cliff_tokens")
                if cliff is not None and req_context > int(cliff):
                    continue
                if int(m.get("context_tokens", 128000)) < req_context:
                    continue

                # Candidate passed all production filters
                selected_model_dict = m
                break

            # If no model matched due to min_intel, relax min_intel while strictly
            # enforcing availability, gating, family exclusions, and price cliff
            if not selected_model_dict and min_intel is not None:
                for mid in candidate_ids:
                    m = self.db.get_model(mid)
                    if not m:
                        continue
                    if int(m.get("available", 1)) == 0 or m.get("status") in ("gated", "retired"):
                        continue
                    if mid.lower().startswith("cxa/") and not mid.lower().startswith("cxa/gpt-6-luna"):
                        continue
                    fam = detect_model_family(mid)
                    if fam in excluded_families:
                        continue
                    cliff = m.get("price_cliff_tokens")
                    if cliff is not None and req_context > int(cliff):
                        continue
                    if int(m.get("context_tokens", 128000)) < req_context:
                        continue
                    selected_model_dict = m
                    break
        else:
            # Direct model requested
            m = self.db.get_model(requested_model)
            if m:
                # Check if model is gated
                if int(m.get("available", 1)) == 0 or m.get("status") in ("gated", "retired"):
                    self.send_error(400, f"Model {requested_model} is gated/unavailable")
                    return
                if requested_model.lower().startswith("cxa/") and not requested_model.lower().startswith("cxa/gpt-6-luna"):
                    self.send_error(400, f"Codex model {requested_model} is not active in production")
                    return
                # Check price cliff
                cliff = m.get("price_cliff_tokens")
                if cliff is not None and req_context > int(cliff):
                    self.send_error(400, f"Token request exceeds price cliff for {requested_model}")
                    return
                selected_model_dict = m

        if not selected_model_dict:
            self.send_error(
                503,
                f"No eligible model found for combo '{requested_model}' satisfying "
                f"exclusions={list(excluded_families)} min_intel={min_intel}",
            )
            return

        selected_model_id = selected_model_dict["model_id"]
        selected_family = detect_model_family(selected_model_id)
        selected_provider = selected_model_dict.get("provider_id") or selected_model_id.split("/")[0]

        # 4. Clamp effort for Sonnet 5.5
        clamped_effort = clamp_effort(selected_model_id, effort_hdr)

        # 5. Build completion response
        resp_payload = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": selected_model_id,
            "system_fingerprint": f"fp_{selected_family}_{selected_provider}",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            f"Production response served by model={selected_model_id} "
                            f"family={selected_family} provider={selected_provider} "
                            f"effort={clamped_effort}"
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 150,
                "completion_tokens": 35,
                "total_tokens": 185,
            },
        }

        resp_bytes = json.dumps(resp_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.send_header("X-OmniRoute-Selected-Model", selected_model_id)
        self.send_header("X-OmniRoute-Selected-Provider", selected_provider)
        self.send_header("X-OmniRoute-Selected-Family", selected_family)
        self.send_header("X-OmniRoute-Clamped-Effort", clamped_effort)
        self.end_headers()
        self.wfile.write(resp_bytes)


class GatewayServerInfo:
    """Handle to running local gateway HTTP server."""
    def __init__(self, server: http.server.ThreadingHTTPServer, port: int, db: CatalogDB):
        self.server = server
        self.port = port
        self.db = db
        self.base_url = f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def live_gateway():
    """Start local ThreadingHTTPServer on an ephemeral port with CatalogDB backend."""
    db = CatalogDB(":memory:")
    db.init_and_seed()
    combos_export = db.export_combos()
    combos_map = {c["name"]: c["models"] for c in combos_export["combos"]}

    # Inject DB and combos into the handler class
    LiveOmniRouteHandler.db = db
    LiveOmniRouteHandler.combos_data = combos_map

    # Bind to ephemeral port 0 on 127.0.0.1
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LiveOmniRouteHandler)
    port = server.server_port

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    gateway_info = GatewayServerInfo(server=server, port=port, db=db)
    try:
        yield gateway_info
    finally:
        server.shutdown()
        server.server_close()
        db.close()


def _http_post_json(url: str, body: dict[str, Any], headers: Mapping[str, str]) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Helper to send HTTP POST and return (status, response_headers, json_body)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, str(v))

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            resp_body = json.loads(resp.read().decode("utf-8"))
            return status, resp_headers, resp_body
    except urllib.error.HTTPError as err:
        err_body = {}
        try:
            err_body = json.loads(err.read().decode("utf-8"))
        except Exception:
            pass
        return err.code, {k: v for k, v in err.headers.items()}, err_body


def _http_get_json(url: str) -> tuple[int, dict[str, Any]]:
    """Helper to send HTTP GET and return (status, json_body)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ==============================================================================
# WORKFLOW 1: S2 NORMAL-RISK IMPLEMENTATION + CROSS-FAMILY REVIEW
# ==============================================================================

def test_production_e2e_normal_risk_implementation_and_cross_family_review(live_gateway: GatewayServerInfo):
    """End-to-End Workflow 1:

    1. measure() -> S2 complexity (180 lines, fanout 3)
    2. evaluate_risk() -> normal risk, 1 reviewer, min intelligence 44.0
    3. resolve_model_for_task() -> selects mimo/mimo-v2.6-pro over qwen3.8-max and paid deepseek
    4. Send implementation request to live HTTP gateway on l2-worker -> serves mimo/mimo-v2.6-pro
    5. resolve_review_plan() with served writer model -> review request sent to SAME l2-worker combo
    6. Verify live gateway excludes writer family (mimo) and serves review on distinct family!
    """
    # 1. Measure task complexity
    m = measure(
        paths=["src/auth/session.py", "tests/test_auth_session.py", "src/auth/tokens.py"],
        lines=180,
        fanout=3,
    )
    assert m.bucket == "S2"
    assert m.should_decompose is False

    # 2. Risk evaluation
    risk_eval = evaluate_risk(
        paths=["src/utils/math.py", "tests/test_math.py"],
        bucket=m.bucket,
        has_tests=True,
    )
    assert risk_eval.risk == "normal"
    assert risk_eval.reviewer_count == 1
    assert risk_eval.min_intelligence == 44.0

    # 3. Model resolution & utility scoring
    resolved = resolve_model_for_task(
        role="implement",
        risk=risk_eval.risk,
        combo_name="l2-worker",
        need_tokens=m.need_tokens,
        db=live_gateway.db,
    )
    assert resolved["model"] == "mimo/mimo-v2.6-pro"
    assert resolved["family"] == "mimo"
    assert resolved["tier"] == "subscription"

    # Verify score ranking: mimo (46.0 intel, $0.13, 92.4% cache) > qwen (45.4, $0.45) > paid deepseek
    m_mimo = live_gateway.db.get_model("mimo/mimo-v2.6-pro")
    m_qwen = live_gateway.db.get_model("opencode-go/qwen3.8-max")
    m_deepseek = live_gateway.db.get_model("deepseek/deepseek-v4-flash-max")

    score_mimo = score_model(m_mimo, role="implement", risk="normal")
    score_qwen = score_model(m_qwen, role="implement", risk="normal")
    score_deepseek = score_model(m_deepseek, role="implement", risk="normal")
    assert score_mimo > score_qwen > score_deepseek

    # 4. Send request to the live HTTP gateway on resolved model
    endpoint = f"{live_gateway.base_url}/v1/chat/completions"
    impl_req_body = {
        "model": resolved["model"],
        "messages": [{"role": "user", "content": "Implement auth session token refresh"}],
    }

    status, resp_headers, resp_data = _http_post_json(
        url=endpoint,
        body=impl_req_body,
        headers=resolved["headers"],
    )

    assert status == 200
    writer_model = resp_headers.get("X-OmniRoute-Selected-Model")
    writer_family = resp_headers.get("X-OmniRoute-Selected-Family")
    writer_provider = resp_headers.get("X-OmniRoute-Selected-Provider")

    assert writer_model == "mimo/mimo-v2.6-pro"
    assert writer_family == "mimo"
    assert writer_provider == "mimo"
    assert resp_data["model"] == "mimo/mimo-v2.6-pro"

    # 5. Feed served writer model into resolve_review_plan()
    rev_plan = resolve_review_plan(
        writer_model=writer_model,
        risk=risk_eval.risk,
        combo_name="l2-worker",
        need_tokens=m.need_tokens,
        db=live_gateway.db,
    )
    assert rev_plan["writer_family"] == "mimo"
    assert len(rev_plan["reviewers"]) == 1

    rev1_spec = rev_plan["reviewers"][0]
    rev1_headers = rev1_spec["headers"]
    assert rev1_headers["X-OmniRoute-Task-Role"] == "review"
    assert "mimo" in rev1_headers["X-OmniRoute-Exclude-Family"]
    assert rev1_headers["X-OmniRoute-Author-Model"] == "mimo/mimo-v2.6-pro"

    # 6. Send review request to the SAME l2-worker combo on the live HTTP gateway
    review_req_body = {
        "model": "l2-worker",
        "messages": [{"role": "user", "content": "Review implementation of session refresh PR"}],
    }

    rev_status, rev_resp_headers, rev_resp_data = _http_post_json(
        url=endpoint,
        body=review_req_body,
        headers=rev1_headers,
    )

    assert rev_status == 200
    rev1_served_model = rev_resp_headers.get("X-OmniRoute-Selected-Model")
    rev1_served_family = rev_resp_headers.get("X-OmniRoute-Selected-Family")

    # Gate verification: Reviewer family is strictly distinct from writer family!
    assert rev1_served_family != writer_family
    assert rev1_served_family in ("claude", "qwen", "deepseek", "glm", "muse")
    assert rev1_served_model != "mimo/mimo-v2.6-pro"


# ==============================================================================
# WORKFLOW 2: S3 HIGH-RISK IMPLEMENTATION + DUAL CROSS-FAMILY REVIEW
# ==============================================================================

def test_production_e2e_high_risk_dual_cross_family_review(live_gateway: GatewayServerInfo):
    """End-to-End Workflow 2:

    When agy/claude-sonnet-5-5-high (claude) is the writer on l2-worker:
      - Reviewer 1 (X-OmniRoute-Exclude-Family: claude) lands on mimo/mimo-v2.6-pro (mimo)
      - Reviewer 2 (X-OmniRoute-Exclude-Family: claude,mimo) lands on a 3rd distinct family (qwen or muse)
      - Proves complete 3-way isolation (writer != rev1 != rev2) over a single shared combo!
    """
    writer_model = "agy/claude-sonnet-5-5-high"
    writer_family = "claude"

    # Resolve dual review plan for high risk
    dual_plan = resolve_review_plan(
        writer_model=writer_model,
        risk="high",
        combo_name="l2-worker",
        need_tokens=32000,
        db=live_gateway.db,
    )

    assert dual_plan["reviewer_count"] == 2
    assert len(dual_plan["reviewers"]) == 2

    rev1_spec = dual_plan["reviewers"][0]
    rev2_spec = dual_plan["reviewers"][1]

    endpoint = f"{live_gateway.base_url}/v1/chat/completions"

    # 1. Dispatch Reviewer 1 request to live gateway on l2-worker
    status1, headers1, data1 = _http_post_json(
        url=endpoint,
        body={"model": "l2-worker", "messages": [{"role": "user", "content": "Reviewer 1 check"}]},
        headers=rev1_spec["headers"],
    )
    assert status1 == 200
    rev1_served_model = headers1.get("X-OmniRoute-Selected-Model")
    rev1_served_family = headers1.get("X-OmniRoute-Selected-Family")

    assert rev1_served_model == "mimo/mimo-v2.6-pro"
    assert rev1_served_family == "mimo"

    # 2. Dispatch Reviewer 2 request to live gateway on SAME l2-worker
    status2, headers2, data2 = _http_post_json(
        url=endpoint,
        body={"model": "l2-worker", "messages": [{"role": "user", "content": "Reviewer 2 check"}]},
        headers=rev2_spec["headers"],
    )
    assert status2 == 200
    rev2_served_model = headers2.get("X-OmniRoute-Selected-Model")
    rev2_served_family = headers2.get("X-OmniRoute-Selected-Family")

    # 3. Assert strict 3-way family isolation: writer != rev1 != rev2
    assert writer_family != rev1_served_family, f"writer ({writer_family}) matches rev1 ({rev1_served_family})"
    assert writer_family != rev2_served_family, f"writer ({writer_family}) matches rev2 ({rev2_served_family})"
    assert rev1_served_family != rev2_served_family, f"rev1 ({rev1_served_family}) matches rev2 ({rev2_served_family})"

    # Reviewer 2 should land on a 3rd distinct family (such as gemini, qwen, or muse)
    assert rev2_served_family in ("gemini", "qwen", "muse", "glm", "deepseek")


# ==============================================================================
# WORKFLOW 3: VENDOR GUARDRAILS, GATING & CLAMPING
# ==============================================================================

def test_production_e2e_vendor_guardrails(live_gateway: GatewayServerInfo):
    """Verify live gateway enforcement of:
      1. Codex ACP luna-only gating (rejecting astra and sol-*)
      2. Claude Sonnet 5.5 max effort clamping -> xhigh
      3. Claude Haiku 100K token price cliff dropping Haiku when tokens > 100000
    """
    endpoint = f"{live_gateway.base_url}/v1/chat/completions"

    # 1. Gated Codex models rejected
    for gated_id in ("cxa/gpt-6-astra-max", "cxa/gpt-6-sol-high", "cxa/gpt-6-sol-max"):
        status, _, err = _http_post_json(
            url=endpoint,
            body={"model": gated_id, "messages": [{"role": "user", "content": "test"}]},
            headers={"X-OmniRoute-Task-Role": "implement"},
        )
        assert status == 400

    # 2. Sonnet 5.5 max effort clamping -> xhigh
    headers_sonnet = {
        "X-OmniRoute-Task-Role": "implement",
        "X-OmniRoute-Effort": "max",
    }
    status_sonnet, resp_hdrs_sonnet, _ = _http_post_json(
        url=endpoint,
        body={"model": "agy/claude-sonnet-5-5-high", "messages": [{"role": "user", "content": "deep analysis"}]},
        headers=headers_sonnet,
    )
    assert status_sonnet == 200
    assert resp_hdrs_sonnet.get("X-OmniRoute-Clamped-Effort") == "xhigh"

    # 3. Haiku price cliff: Haiku eligible under 100K, rejected over 100K
    status_haiku_under, _, _ = _http_post_json(
        url=endpoint,
        body={"model": "claude/claude-haiku-4-5", "messages": [{"role": "user", "content": "summary"}]},
        headers={"X-OmniRoute-Required-Context": "50000"},
    )
    assert status_haiku_under == 200

    status_haiku_over, _, _ = _http_post_json(
        url=endpoint,
        body={"model": "claude/claude-haiku-4-5", "messages": [{"role": "user", "content": "summary"}]},
        headers={"X-OmniRoute-Required-Context": "120000"},
    )
    assert status_haiku_over == 400


# ==============================================================================
# WORKFLOW 4: MCP SERVER PRODUCTION INTEGRATION
# ==============================================================================

class ProductionGatewayChildRunner(SpawnerRunner):
    """SpawnerRunner that spawns a real Python child process querying the live HTTP gateway."""

    def __init__(self, gateway_url: str):
        super().__init__()
        self.gateway_url = gateway_url

    async def spawn(
        self,
        prompt: str,
        model_or_combo: str = "l2-worker",
        role: str = "implement",
        risk: str = "normal",
        headers: Mapping[str, str] | None = None,
        dry_run: bool = False,
        cwd: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        gateway: bool = True,
        gateway_online: bool = True,
        wait_seconds: float = 0.0,
        client: str | None = None,
    ) -> JobInfo:
        if dry_run:
            return await super().spawn(
                prompt=prompt,
                model_or_combo=model_or_combo,
                role=role,
                risk=risk,
                headers=headers,
                dry_run=True,
                cwd=cwd,
                extra_env=extra_env,
                gateway=gateway,
                gateway_online=gateway_online,
                wait_seconds=wait_seconds,
            )

        job_id = f"job-{uuid.uuid4().hex[:8]}"
        work_dir = cwd or self.default_cwd

        envelope_headers = dict(headers or {})
        if not envelope_headers:
            envelope_headers = build_routing_envelope_headers(
                role=role,
                risk=risk,
                min_intelligence=44.0,
                required_context=32000,
            )

        # Real Python script executed as child process, connecting to the live gateway
        child_script = (
            "import urllib.request, json, os, sys\n"
            f"gw_url = {self.gateway_url!r} + '/v1/chat/completions'\n"
            f"hdrs = {json.dumps(envelope_headers)}\n"
            f"req_data = json.dumps({{'model': {model_or_combo!r}, 'messages': [{{'role': 'user', 'content': {prompt!r}}}]}}).encode('utf-8')\n"
            "req = urllib.request.Request(gw_url, data=req_data, headers={'Content-Type': 'application/json', **hdrs})\n"
            "try:\n"
            "    with urllib.request.urlopen(req, timeout=10) as r:\n"
            "        mod = r.headers.get('X-OmniRoute-Selected-Model', '')\n"
            "        fam = r.headers.get('X-OmniRoute-Selected-Family', '')\n"
            "        print(f'X-OmniRoute-Selected-Model: {mod}')\n"
            "        print(f'X-OmniRoute-Selected-Family: {fam}')\n"
            "        print(r.read().decode('utf-8'))\n"
            "except Exception as e:\n"
            "    sys.stderr.write(str(e))\n"
            "    sys.exit(1)\n"
        )

        child_env = build_child_env(
            {"OMNIROUTE_BASE_URL": f"{self.gateway_url}/v1", **(extra_env or {})},
            headers=envelope_headers,
        )

        cmd = [sys.executable, "-c", child_script]

        job = JobInfo(
            job_id=job_id,
            status="running",
            task_prompt=prompt,
            model_or_combo=model_or_combo,
            role=role,
            risk=risk,
            command=cmd,
            dry_run=False,
        )
        self.jobs[job_id] = job
        # Only the in-process transport adapter may witness the identity. Child
        # stdout remains untrusted even when it looks like gateway headers.
        self._adapter_tokens[job_id] = object()
        request = urllib.request.Request(
            self.gateway_url + '/v1/chat/completions',
            data=json.dumps({'model': model_or_combo, 'messages': [
                {'role': 'user', 'content': prompt}]}).encode(),
            headers={'Content-Type': 'application/json', **envelope_headers})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.served_witness(job_id, response.headers['X-OmniRoute-Selected-Model'],
                                adapter_token=self._adapter_tokens[job_id])

        proc = await asyncio.create_subprocess_exec(
            cmd[0],
            *cmd[1:],
            cwd=work_dir,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self._processes[job_id] = proc

        task = asyncio.create_task(self._monitor_process(job_id, proc, model_or_combo))
        self._tasks[job_id] = task

        if wait_seconds > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

        return self.jobs[job_id]


def test_production_e2e_mcp_server_workflow(live_gateway: GatewayServerInfo):
    """Verify MCPSpawnerServer production tools against live gateway with real child execution:
      - plangraph_agent_health
      - plangraph_agent_query_catalog
      - plangraph_agent_spawn (invoking child process against live gateway)
      - plangraph_agent_status (verifying served_model and served_family are captured)
      - plangraph_agent_steer
    """
    async def _test():
        runner = ProductionGatewayChildRunner(gateway_url=live_gateway.base_url)
        mcp_server = MCPSpawnerServer(runner=runner)

        # 1. Health check
        health_resp = await mcp_server.call_tool("plangraph_agent_health", {})
        assert health_resp["status"] == "healthy"
        assert health_resp["spawner_version"] == "2.0.0"
        assert health_resp["catalog_models_count"] >= 22

        # 2. Query catalog tool
        catalog_query = await mcp_server.call_tool(
            "plangraph_agent_query_catalog",
            {"combo": "l2-worker", "family": "mimo", "min_intelligence": 45.0},
        )
        assert catalog_query["count"] >= 1
        mimo_ids = [m["id"] for m in catalog_query["models"]]
        assert "mimo/mimo-v2.6-pro" in mimo_ids

        # 3. Spawn agent task (executing child process communicating with live HTTP gateway)
        spawn_resp = await mcp_server.call_tool(
            "plangraph_agent_spawn",
            {
                "prompt": "Implement distributed rate limiter",
                    "model_or_combo": "mimo/mimo-v2.6-pro",
                    "card": {"estimate_tokens": 1000},
                    "override_reason": "Hermetic gateway telemetry contract test",
                "role": "implement",
                "risk": "normal",
                "dry_run": False,
                "wait_seconds": 8.0,
            },
        )
        job_id = spawn_resp["job_id"]
        assert job_id.startswith("job-")

        # 4. Check status tool and verify served telemetry extraction
        status_resp = await mcp_server.call_tool(
            "plangraph_agent_status",
            {"job_id": job_id},
        )
        assert status_resp["job_id"] == job_id
        assert status_resp["status"] == "completed"
        assert status_resp["exit_code"] == 0

        # Critical invariant: served_model and served_family captured from live gateway response headers
        assert status_resp["served_model"] == "mimo/mimo-v2.6-pro"
        assert status_resp["served_family"] == "mimo"

        # 5. Steer tool
        steer_resp = await mcp_server.call_tool(
            "plangraph_agent_steer",
            {"job_id": job_id, "message": "Enforce sliding window token bucket"},
        )
        assert steer_resp == {"ok": False, "steered": False, "error": "no stdin"}
        assert "Enforce sliding window token bucket" in runner.jobs[job_id].steer_messages

    asyncio.run(_test())
