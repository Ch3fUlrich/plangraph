import json
import re
import pytest
from pathlib import Path

from plangraph.spawner.mimo_policy import check_mimo_path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "opencode.jsonc"


@pytest.mark.parametrize("settings,expected", [
    ({}, (60000, 60000)),
    ({"headerTimeout": False, "chunkTimeout": 900000}, (60000, 60000)),
    ({"headerTimeout": 1000, "chunkTimeout": 2000}, (1000, 2000)),
    ({"headerTimeout": 1234.75, "chunkTimeout": 900000.5}, (1234, 60000)),
    ({"headerTimeout": 0.5, "chunkTimeout": 0.25}, (1, 1)),
    ({"headerTimeout": -5, "chunkTimeout": 0}, (60000, 60000)),
])
def test_gateway_gemini_seat_bounds_silent_requests(settings, expected):
    from plangraph.routing_support.opencode_render import render_gateway_providers
    env = {"OMNIROUTE_BASE_URL": "http://gateway.invalid",
           "OPENCODE_CONFIG_CONTENT": json.dumps({"providers": {
               "omniroute": {"settings": settings, "models": {"agy/gemini-3.8-flash-high": {}}},
               "other": {"settings": {"headerTimeout": False}}}})}
    assert render_gateway_providers(env, "omniroute/agy/gemini-3.8-flash-high") is None
    providers = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    rendered = providers["omniroute"]["settings"]
    for key, want in zip(("headerTimeout", "chunkTimeout"), expected):
        assert type(rendered[key]) is int
        assert rendered[key] == want
    assert providers["other"]["settings"]["headerTimeout"] is False


def test_gateway_gemini_exact_leg_model_sets_both_bounds():
    from plangraph.routing_support.opencode_render import render_gateway_providers
    config = load_config()
    config["providers"]["omniroute"]["settings"] = {"baseURL": "test-endpoint"}
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}
    # Isolate timeout rendering from endpoint validation.
    from unittest.mock import patch
    with patch("plangraph.routing_support.opencode_render._is_valid_url", return_value=True):
        assert render_gateway_providers(env, "omniroute/agy/gemini-3.8-flash-high") is None
    settings = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]["omniroute"]["settings"]
    assert settings["headerTimeout"] == 60000
    assert settings["chunkTimeout"] == 60000


def strip_jsonc_comments(text: str) -> str:
    """Strips // comments outside of string literals."""
    pattern = r'("(?:[^"\\]|\\.)*")|//.*'
    return re.sub(pattern, lambda m: m.group(1) if m.group(1) else "", text)


def load_config() -> dict:
    assert CONFIG_PATH.is_file(), f"Missing config file at {CONFIG_PATH}"
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    stripped = strip_jsonc_comments(raw)
    return json.loads(stripped)


def iter_values(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from iter_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_values(item)
    elif isinstance(obj, str):
        yield obj


def test_agents_exist_with_model():
    cfg = load_config()
    assert "model" not in cfg, "opencode.jsonc should not have top-level 'model' key"
    assert "agents" in cfg, "opencode.jsonc missing 'agents' block"
    agents = cfg["agents"]

    assert "t2-worker" in agents, "Agent 't2-worker' not found in agents"
    assert "l2-worker" in agents, "Agent 'l2-worker' not found in agents"

    assert agents["t2-worker"]["model"] == "mimo/mimo-v2.6-flash"
    assert agents["l2-worker"]["model"] == "mimo/mimo-v2.6-flash"
    assert agents["t2-worker"] == agents["l2-worker"], "t2-worker and l2-worker must have identical definitions"


def test_all_config_model_ids_follow_mimo_policy():
    cfg = load_config()
    model_ids = [agent["model"] for agent in cfg["agents"].values() if "model" in agent]
    model_ids.extend(
        model["modelID"]
        for provider in cfg["providers"].values()
        for model in provider.get("models", {}).values()
    )
    for model_id in model_ids:
        assert check_mimo_path(model_id) == model_id


def test_provider_omniroute():
    cfg = load_config()
    assert "providers" in cfg, "opencode.jsonc missing 'providers' block"
    providers = cfg["providers"]

    assert "omniroute" in providers, "Provider 'omniroute' not found in providers"
    omniroute = providers["omniroute"]

    assert "AUTOOS_OMNIROUTE_KEY" in omniroute.get("env", [])
    assert omniroute.get("package") == "@opencode/ai/providers/openai-compatible"
    assert omniroute.get("settings", {}).get("baseURL") == "{env:PLANGRAPH_REVIEW_URL}"

    # Retain the gateway provider, but never register MiMo models under it.
    assert "models" not in omniroute, "Unused empty model registry must be omitted"


def test_provider_mimo():
    cfg = load_config()
    flash, pro = (value.split('/', 1)[1] for value in
                  ('mimo/mimo-v2.6-flash', 'mimo/mimo-v2.6-pro'))
    assert cfg["providers"]["mimo"] == {
        "package": "@opencode/ai/providers/openai-compatible",
        "name": "MiMo direct gateway legs (D-2459)",
        "env": ["AUTOOS_OMNIROUTE_KEY"],
        "settings": {"baseURL": "{env:PLANGRAPH_REVIEW_URL}"},
        "models": {
            flash: {
                "modelID": "mimo/mimo-v2.6-flash",
                "name": "Xiaomi MiMo v2.6 Flash",
                "limit": {"context": 1000000, "output": 65536},
            },
            pro: {"modelID": "mimo/mimo-v2.6-pro", "name": "Xiaomi MiMo v2.6 Pro",
                  "limit": {"context": 1000000, "output": 65536}},
        },
    }


@pytest.mark.parametrize("review_url_set", [False, True])
@pytest.mark.parametrize("source", ["OMNIROUTE_BASE_URL", "PLANGRAPH_GATEWAY_URL", "config"])
@pytest.mark.parametrize("model", ["mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro"])
@pytest.mark.parametrize("path", ["", "/mimo/v1/"])
def test_mimo_overlay_uses_configured_gateway(monkeypatch, tmp_path, caplog,
                                            review_url_set, source, model, path):
    from plangraph.review.gateway import GATEWAY_ENV_VAR
    from plangraph.routing_support.clients import build_child_env
    from plangraph.routing_support.opencode_overlay import overlay_env
    from plangraph.routing_support.opencode_render import render_gateway_providers

    for name in (GATEWAY_ENV_VAR, "OMNIROUTE_BASE_URL", "PLANGRAPH_GATEWAY_URL",
                 "AUTOOS_OMNIROUTE_URL", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    gateway = "https://gateway.invalid" + path
    if review_url_set:
        monkeypatch.setenv(GATEWAY_ENV_VAR, "https://review.invalid/v1")
    config = {"gateway_url": gateway} if source == "config" else None
    if config is None:
        monkeypatch.setenv(source, gateway)
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path))
    root_config = tmp_path / "opencode.jsonc"
    root_config.write_text(json.dumps(load_config()), encoding="utf-8")
    sentinel = "fixture-gateway-key-do-not-render"
    monkeypatch.setenv("AUTOOS_OMNIROUTE_KEY", sentinel)
    env = overlay_env(build_child_env(config=config))
    overlaid = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    assert overlaid["omniroute"]["settings"]["baseURL"] == gateway

    reason = render_gateway_providers(env, model)
    assert reason is None
    providers = json.loads(env["OPENCODE_CONFIG_CONTENT"])["providers"]
    expected = gateway.rstrip("/") if path else gateway + "/v1"
    assert providers["mimo"]["settings"]["baseURL"] == expected
    assert providers["omniroute"]["settings"]["baseURL"] == expected
    assert providers["mimo"]["models"][model.split("/", 1)[1]]["modelID"] == model
    assert providers["mimo"]["env"] == ["AUTOOS_OMNIROUTE_KEY"]
    assert env["AUTOOS_OMNIROUTE_KEY"] == sentinel
    assert sentinel not in env["OPENCODE_CONFIG_CONTENT"]
    assert sentinel not in root_config.read_text(encoding="utf-8")
    assert sentinel not in caplog.text


@pytest.mark.parametrize("url", ["https://user:password@gateway.invalid/v1",
                               "https://gateway.invalid/v1?key=fixture", "file:///gateway"])
def test_mimo_config_gateway_rejects_invalid_url(monkeypatch, tmp_path, url):
    from plangraph.review.gateway import GATEWAY_ENV_VAR
    from plangraph.routing_support.clients import build_child_env
    from plangraph.routing_support.opencode_overlay import overlay_env
    from plangraph.routing_support.opencode_render import REFUSAL_UNRESOLVED, render_gateway_providers

    for name in (GATEWAY_ENV_VAR, "OMNIROUTE_BASE_URL", "PLANGRAPH_GATEWAY_URL",
                 "AUTOOS_OMNIROUTE_URL", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTOOS_ROOT", str(tmp_path))
    (tmp_path / "opencode.jsonc").write_text(json.dumps(load_config()), encoding="utf-8")
    env = overlay_env(build_child_env(config={"gateway_url": url}))
    reason = render_gateway_providers(env, "mimo/mimo-v2.6-pro")
    assert reason == REFUSAL_UNRESOLVED


def test_secret_deny_rules_exist():
    cfg = load_config()
    assert "permissions" in cfg, "opencode.jsonc missing 'permissions' block"
    perms = cfg["permissions"]

    # Assert read denies and external_directory denies exist for secret files
    read_denies = {
        p["resource"]
        for p in perms
        if p.get("action") == "read" and p.get("effect") == "deny"
    }
    ext_denies = {
        p["resource"]
        for p in perms
        if p.get("action") == "external_directory" and p.get("effect") == "deny"
    }
    grep_denies = {
        p["resource"]
        for p in perms
        if p.get("action") == "grep" and p.get("effect") == "deny"
    }
    glob_denies = {
        p["resource"]
        for p in perms
        if p.get("action") == "glob" and p.get("effect") == "deny"
    }

    secret_patterns = {
        "*.env",
        "*.env.*",
        "*auth.json",
        "*api_keys*",
        "*api-keys*",
        "*client.key*",
        "*manage.key*",
        "*.claude.json*",
    }

    assert secret_patterns.issubset(read_denies), f"Missing read denies: {secret_patterns - read_denies}"
    assert secret_patterns.issubset(ext_denies), f"Missing external_directory denies: {secret_patterns - ext_denies}"
    assert "*.env" in grep_denies, "Missing grep deny: *.env"
    assert "*api-keys*" in grep_denies, "Missing grep deny: *api-keys*"
    assert "*.env" in glob_denies, "Missing glob deny: *.env"
    assert "*api-keys*" in glob_denies, "Missing glob deny: *api-keys*"


def test_no_credential_like_keys():
    key_regex = re.compile(r"sk-[A-Za-z0-9]{8,}")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    assert not key_regex.search(raw), "Credential-like key found in raw opencode.jsonc"

    cfg = load_config()
    for val in iter_values(cfg):
        assert not key_regex.search(str(val)), f"Credential-like key found in config value: {val}"
