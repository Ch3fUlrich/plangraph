"""Host admission probes the per-run rendered gateway config (SPAWN-WIN-PARITY S1 fix, D-2864)."""
from __future__ import annotations

import asyncio
import json

import pytest

from plangraph.routing_support import opencode_render
from plangraph.routing_support import resolver as r
from plangraph.routing_support.opencode_render import REFUSAL_UNRESOLVED


@pytest.fixture(autouse=True)
def nt_host_without_root(monkeypatch, tmp_path):
    for var in ("OMNIROUTE_BASE_URL", "PLANGRAPH_GATEWAY_URL", "AUTOOS_OMNIROUTE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path / "absent-root"))
    monkeypatch.setattr(opencode_render, "_is_nt", lambda: True)


def _listing(seen):  # stand-in for `opencode models` on an NT-like host without AutoOS root
    def probe(child):
        providers = json.loads(child.get("OPENCODE_CONFIG_CONTENT", "{}")).get("providers", {})
        seen.append(providers)
        return {f"{p}/{m}" for p, block in providers.items() for m in (block.get("models") or {})}
    return probe


def test_mimo_leg_admitted_when_gateway_url_set(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:9/v1")
    row = {"harness": "opencode", "model": "mimo/mimo-v2.6-pro"}
    # A keyless `opencode models` gets 401 from the gateway and lists no mimo rows (D-2868).
    assert r.host_skip_reason(row, probe=lambda child: {"omniroute/x"}) is None


def test_async_admission_lists_rendered_config_and_never_leaks_key(monkeypatch, caplog):
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("AUTOOS_OMNIROUTE_KEY", "sk-sentinel-d2868")
    seen = []
    monkeypatch.setattr(r, "_served_models_real", lambda config, **kw: seen.append(config) or {"omniroute/x"})
    row = {"harness": "opencode", "model": "mimo/mimo-v2.6-pro"}
    with caplog.at_level("DEBUG"):
        assert asyncio.run(r.host_skip_reason_async(row)) is None
    assert seen and json.loads(seen[0])["providers"]["mimo"]["settings"]["baseURL"] == "http://127.0.0.1:9/v1"
    assert "sk-sentinel-d2868" not in seen[0] + caplog.text


def test_mimo_leg_without_base_url_refused_before_probe():
    seen = []
    row = {"harness": "opencode", "model": "mimo/mimo-v2.6-pro"}
    assert r.host_skip_reason(row, probe=_listing(seen)) == REFUSAL_UNRESOLVED
    assert seen == []
