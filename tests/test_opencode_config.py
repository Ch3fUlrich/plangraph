import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "opencode.jsonc"


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
    assert "agents" in cfg, "opencode.jsonc missing 'agents' block"
    agents = cfg["agents"]

    assert "t2-worker" in agents, "Agent 't2-worker' not found in agents"
    assert "l2-worker" in agents, "Agent 'l2-worker' not found in agents"

    assert agents["t2-worker"]["model"] == "omniroute/mimo-v2.6-flash"
    assert agents["l2-worker"]["model"] == "omniroute/mimo-v2.6-flash"
    assert agents["t2-worker"] == agents["l2-worker"], "t2-worker and l2-worker must have identical definitions"


def test_provider_omniroute():
    cfg = load_config()
    assert "providers" in cfg, "opencode.jsonc missing 'providers' block"
    providers = cfg["providers"]

    assert "omniroute" in providers, "Provider 'omniroute' not found in providers"
    omniroute = providers["omniroute"]

    assert "AUTOOS_OMNIROUTE_KEY" in omniroute.get("env", [])
    assert omniroute.get("package") == "@opencode/ai/providers/openai-compatible"
    assert omniroute.get("settings", {}).get("baseURL") == "http://127.0.0.1:20128/v1"

    models = omniroute.get("models", {})
    assert "mimo-v2.6-flash" in models, "Model 'mimo-v2.6-flash' not found under omniroute"
    mimo = models["mimo-v2.6-flash"]
    assert mimo.get("modelID") == "mimo-v2.6-flash"


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


def test_no_credential_like_keys():
    key_regex = re.compile(r"sk-[A-Za-z0-9]{8,}")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    assert not key_regex.search(raw), "Credential-like key found in raw opencode.jsonc"

    cfg = load_config()
    for val in iter_values(cfg):
        assert not key_regex.search(str(val)), f"Credential-like key found in config value: {val}"
