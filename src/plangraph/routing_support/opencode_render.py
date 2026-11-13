"""Per-run gateway provider config rendering for opencode children (D-2791 S1).

Renders gateway provider blocks (mimo, omniroute) into OPENCODE_CONFIG_CONTENT
per invariants I1-I8: no credentials, base URL precedence, fail-closed refusal.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

from plangraph.mimo_policy import DIRECT_MIMO_IDS

REFUSAL_UNRESOLVED = (
    "not_served_on_host: gateway_base_url_unresolved "
    "(set OMNIROUTE_BASE_URL or PLANGRAPH_GATEWAY_URL)"
)
REFUSAL_TOO_LARGE = "config_too_large: OPENCODE_CONFIG_CONTENT"
GATEWAY_KEY_NAME = "AUTOOS_OMNIROUTE_KEY"


def _is_nt() -> bool:
    return os.name == "nt"


def _env_names(provider: dict[str, Any]) -> list[Any]:
    """A provider block's env names; any non-list `env` is treated as absent.

    H5 never-raise posture: `"env": null` (or any other non-list) must not make
    `GATEWAY_KEY_NAME in ...` raise TypeError out of the render.
    """
    env = provider.get("env")
    return env if isinstance(env, list) else []


def _is_valid_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    raw = url.strip()
    # A bare trailing '?' or '#' yields an empty query/fragment in urlsplit;
    # refuse any raw query or fragment delimiter outright (I5).
    if "?" in raw or "#" in raw:
        return False
    try:
        p = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return False
    if "@" in p.netloc or p.username or p.password or p.query or p.fragment:
        return False
    return True


def _normalize_gateway_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip().rstrip("/"))
    path = p.path if p.path not in ("", "/") else "/v1"
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, "", ""))


def _mimo_template() -> dict[str, Any]:
    return {
        "package": "@opencode/ai/providers/openai-compatible",
        "name": "MiMo direct gateway legs (D-2459)",
        "env": [GATEWAY_KEY_NAME],
        "settings": {},
        "models": {
            mid.split("/", 1)[1]: {
                "modelID": mid,
                "name": f"Xiaomi MiMo v2.6 {'Pro' if 'pro' in mid else 'Flash'}",
                "limit": {"context": 1000000, "output": 65536},
            }
            for mid in DIRECT_MIMO_IDS
        },
    }


#: Limit for a cx effort variant whose base model the root config does not declare.
_CX_DEFAULT_LIMIT = {"context": 400000, "output": 128000}


def _declare_cx_effort_model(providers: dict[str, Any], model: str) -> None:
    """Declare ``omniroute/cx-<m>-<effort>`` for this run as OmniRoute ``cx/<m>-<effort>``.

    Spawned opencode runs see only OPENCODE_CONFIG_CONTENT (opencode_isolation
    closes the global layer), and the AutoOS root config declares only the base
    cx models (``cx-gpt-6.1-sol`` -> ``cx/gpt-6.1-sol``).  opencode refuses an
    undeclared model ("provider.no-route", measured 2026-11-09).  The variant
    takes its base model's limit; an existing entry is kept as is.
    """
    from plangraph.planner.legs import CX_EFFORTS
    base, _, effort = model.rpartition("-")
    if not model.startswith("omniroute/cx-") or effort not in CX_EFFORTS:
        return
    omniroute = providers.setdefault("omniroute", {})
    models = omniroute.setdefault("models", {}) if isinstance(omniroute, dict) else None
    key, base_key = model[len("omniroute/"):], base[len("omniroute/"):]
    if not isinstance(models, dict) or key in models:
        return
    base_entry = models.get(base_key)
    limit = base_entry.get("limit") if isinstance(base_entry, dict) else None
    models[key] = {"modelID": "cx/" + key[len("cx-"):], "name": key,
                   "limit": dict(limit) if isinstance(limit, dict) else dict(_CX_DEFAULT_LIMIT)}


def render_gateway_providers(
    env: dict[str, str], model: str, *, is_nt: bool | None = None
) -> str | None:
    """Mutate env['OPENCODE_CONFIG_CONTENT'] with gateway provider config."""
    if "_PLANGRAPH_OVERLAY_UNKNOWN" in env:
        return None
    raw_cfg = env.get("OPENCODE_CONFIG_CONTENT", "{}")
    try:
        config = json.loads(raw_cfg) if raw_cfg else {}
        if not isinstance(config, dict):
            return None
    except (ValueError, TypeError):
        return None

    model_clean = model.split("#", 1)[0].strip() if isinstance(model, str) else ""
    provider_prefix = model_clean.split("/", 1)[0].strip() if "/" in model_clean else ""
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        return None

    prov_block = providers.get(provider_prefix)
    has_gw_key = isinstance(prov_block, dict) and GATEWAY_KEY_NAME in _env_names(prov_block)
    if provider_prefix not in ("mimo", "omniroute") and not has_gw_key:
        return None

    if "mimo" not in providers and model_clean in DIRECT_MIMO_IDS:
        providers["mimo"] = _mimo_template()

    # Step 4: Honour the already-configured child endpoint before env fallbacks.
    # Read the parsed config directly: no clients import or eager fallback call.
    omniroute = providers.get("omniroute")
    child_settings = omniroute.get("settings") if isinstance(omniroute, dict) else None
    resolved_url = None
    for cand in (
        child_settings.get("baseURL") if isinstance(child_settings, dict) else None,
        env.get("OMNIROUTE_BASE_URL"),
        env.get("PLANGRAPH_GATEWAY_URL"),
        env.get("AUTOOS_OMNIROUTE_URL"),  # transitional: drop with AO-SPAWNER-REMOVE (D-2800)
    ):
        if isinstance(cand, str) and cand.strip() and _is_valid_url(cand):
            resolved_url = _normalize_gateway_url(cand)
            break

    # Step 5: Fill missing env on gateway providers
    for p_name, p_val in providers.items():
        if isinstance(p_val, dict):
            if p_name in ("mimo", "omniroute") or GATEWAY_KEY_NAME in _env_names(p_val):
                if not isinstance(p_val.get("env"), list):
                    p_val["env"] = [GATEWAY_KEY_NAME]

    if resolved_url:
        for p_val in providers.values():
            if isinstance(p_val, dict) and GATEWAY_KEY_NAME in _env_names(p_val):
                settings = p_val.setdefault("settings", {})
                if isinstance(settings, dict):
                    settings["baseURL"] = resolved_url
    else:
        active_block = providers.get(provider_prefix)
        existing = (
            active_block.get("settings", {}).get("baseURL")
            if isinstance(active_block, dict) and isinstance(active_block.get("settings"), dict)
            else None
        )
        if not (isinstance(existing, str) and existing.strip()
                and "{env:" not in existing and _is_valid_url(existing)):
            return REFUSAL_UNRESOLVED
        # The /v1 rule applies to a root-supplied URL exactly as to an override.
        active_block["settings"]["baseURL"] = _normalize_gateway_url(existing)

    # D-5066: a silent gateway must not consume the seat's 25-minute cap.
    # Headers alone do not guarantee a first token: also bound silent SSE gaps.
    # OpenCode applies these controls provider-wide and retries transport errors.
    if provider_prefix == "omniroute" and model_clean.startswith("omniroute/agy/gemini-"):
        block = providers.get(provider_prefix)
        if isinstance(block, dict):
            settings = block.setdefault("settings", {})
            if isinstance(settings, dict):
                for key in ("headerTimeout", "chunkTimeout"):
                    value = settings.get(key)
                    settings[key] = max(1, int(min(value, 60000))) if type(value) in (int, float) and value > 0 else 60000

    _declare_cx_effort_model(providers, model_clean)
    rendered = json.dumps(config)
    nt_check = _is_nt() if is_nt is None else is_nt
    if nt_check and len(rendered) > 30000:
        return REFUSAL_TOO_LARGE
    env["OPENCODE_CONFIG_CONTENT"] = rendered
    return None
