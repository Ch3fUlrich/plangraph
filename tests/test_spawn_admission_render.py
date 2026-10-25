"""Host admission probes the per-run rendered gateway config (SPAWN-WIN-PARITY S1 fix, D-2864)."""
from __future__ import annotations

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
    seen = []
    row = {"harness": "opencode", "model": "mimo/mimo-v2.6-pro"}
    assert r.host_skip_reason(row, probe=_listing(seen)) is None
    assert seen and seen[0]["mimo"]["settings"]["baseURL"] == "http://127.0.0.1:9/v1"


def test_mimo_leg_without_base_url_refused_before_probe():
    seen = []
    row = {"harness": "opencode", "model": "mimo/mimo-v2.6-pro"}
    assert r.host_skip_reason(row, probe=_listing(seen)) == REFUSAL_UNRESOLVED
    assert seen == []
