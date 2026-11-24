"""Mimo legs run through OmniRoute provider xiaomi-mimo (LE-MIMO-LEG-CAPPED-200K-PROVIDER-SAYS-1M, G)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state
from plangraph.routing_support.catalog_seed import omniroute_provider_id
from plangraph.routing_support.opencode_render import render_gateway_providers
from plangraph.spawner.gateway_models import PROVIDER_ALIASES

REGISTRY = Path(__file__).resolve().parents[1] / "configuration/opencode/providers.json"
MIMO_LEGS = ("mimo/mimo-v2.6-pro", "mimo/mimo-v2.6-flash")


def _reader(tmp_path, snapshot):
    provider = SimpleNamespace(snapshot=lambda: snapshot)
    store = leg_state.LegStateStore(tmp_path / "state")
    return leg_state.LegAdapter(store, provider, lambda: list(MIMO_LEGS), enabled=True, clock=lambda: 0)


def test_one_mapping_from_the_leg_prefix_to_the_omniroute_provider():
    assert omniroute_provider_id("mimo") == "xiaomi-mimo"
    assert omniroute_provider_id("opencode-go") == "opencode-go"
    assert PROVIDER_ALIASES["xiaomi-mimo"] == "mimo"   # the call-log alias is the same row, reversed


@pytest.mark.parametrize("leg", MIMO_LEGS)
def test_snapshot_with_xiaomi_mimo_makes_both_mimo_legs_reachable(tmp_path, leg):
    reader = _reader(tmp_path, {"xiaomi-mimo": SimpleNamespace(available=True, stale=False)})
    assert reader(leg)["state"] == "ok"


@pytest.mark.parametrize("leg", MIMO_LEGS)
def test_missing_provider_is_reported_with_the_id_it_looked_for(tmp_path, leg):
    reader = _reader(tmp_path, {"mimo": SimpleNamespace(available=True, stale=False)})   # the old, wrong id
    status = reader(leg)
    assert status["state"] == "down"
    assert status["reason"] == "unreachable"
    assert status["evidence"] == {"looked_up_provider": "xiaomi-mimo"}


def test_mimo_provider_takes_the_omniroute_base_url_and_key_env():
    providers = json.loads(REGISTRY.read_text())["providers"]
    assert providers["mimo"]["env"] == providers["omniroute"]["env"]
    assert providers["mimo"]["settings"]["baseURL"] == providers["omniroute"]["settings"]["baseURL"]


def test_rendered_mimo_provider_follows_the_omniroute_endpoint():
    config = {"providers": json.loads(REGISTRY.read_text())["providers"]}
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(config), "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128/v1"}
    config["providers"]["omniroute"]["settings"].pop("baseURL")
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    assert render_gateway_providers(env, "mimo/mimo-v2.6-pro") is None
    rendered = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    assert rendered["mimo"]["settings"]["baseURL"] == rendered["omniroute"]["settings"]["baseURL"] == "http://127.0.0.1:20128/v1"
