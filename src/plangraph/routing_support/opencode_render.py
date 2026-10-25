"""Per-run gateway provider config rendering for opencode children (D-2791 S1).

Renders gateway provider blocks (mimo, omniroute) into OPENCODE_CONFIG_CONTENT
per invariants I1-I8: no credentials, base URL precedence, fail-closed refusal.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

from plangraph.mimo_policy import ACCEPTED_MIMO_IDS

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
    try:
        p = urllib.parse.urlsplit(url.strip())
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
            for mid in ACCEPTED_MIMO_IDS
        },
    }


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

    if "mimo" not in providers and model_clean in ACCEPTED_MIMO_IDS:
        providers["mimo"] = _mimo_template()

    # Step 4: Resolve base URL in I4 order
    resolved_url = None
    for var in (
        "OMNIROUTE_BASE_URL",
        "PLANGRAPH_GATEWAY_URL",
        "AUTOOS_OMNIROUTE_URL",  # transitional: drop with AO-SPAWNER-REMOVE (D-2800)
    ):
        cand = env.get(var)
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

    rendered = json.dumps(config)
    env["OPENCODE_CONFIG_CONTENT"] = rendered
    nt_check = _is_nt() if is_nt is None else is_nt
    if nt_check and len(rendered) > 30000:
        return REFUSAL_TOO_LARGE
    return None
