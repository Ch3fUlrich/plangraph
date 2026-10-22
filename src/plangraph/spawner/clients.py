"""OmniRoute ACP-First Client Adapter with Tailnet Coexistence.

All model providers route first through OmniRoute's ACP/Gateway (http://127.0.0.1:20128/v1)
via opencode run (or omniroute run). Standalone direct CLI spawning (agy, qodercli, codex,
claude) is reserved strictly as an offline emergency fallback when gateway_online=False.

Preserves Windows SystemRoot/COMSPEC/PATHEXT/TEMP environment variables (D-2090 BUG-B).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

DEFAULT_GATEWAY_URL = "http://127.0.0.1:20128/v1"


@dataclass(frozen=True)
class Client:
    """Client CLI adapter specification."""
    name: str
    binary: str
    headless: bool = True
    gateway: bool = True
    auth_env: str = "AUTOOS_OMNIROUTE_KEY"
    notes: str = ""
    direct_cmd_template: Sequence[str] = field(default_factory=tuple)


CLIENTS: dict[str, Client] = {
    "opencode": Client("opencode", "opencode", headless=True, gateway=True, notes="OmniRoute ACP first"),
    "claude": Client("claude", "claude", headless=True, gateway=True, notes="Claude client via OmniRoute ACP"),
    "agy": Client("agy", "agy", headless=True, gateway=True, notes="Antigravity via OmniRoute ACP"),
    "qoder": Client("qoder", "qodercli", headless=True, gateway=True, notes="Qoder via OmniRoute ACP"),
    "codex": Client("codex", "codex", headless=True, gateway=True, notes="Codex via OmniRoute ACP"),
    "qwen": Client("qwen", "qwen", headless=True, gateway=True, notes="Qwen via OmniRoute ACP"),
    "gemini": Client("gemini", "gemini", headless=True, gateway=True, notes="Gemini via OmniRoute ACP"),
}


def build_child_env(
    extra_env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build subprocess environment preserving critical OS and Tailnet variables.

    On Windows (os.name == 'nt'), ALWAYS preserves:
      - SystemRoot / SYSTEMROOT
      - COMSPEC
      - PATHEXT
      - TEMP (and TMP)
    preventing the Windows opencode serve port-in-use crash (D-2090 BUG-B).
    """
    import json as _json

    env = dict(os.environ)

    if os.name == "nt":
        critical_windows_vars = (
            "SystemRoot",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PATH",
        )
        for key in critical_windows_vars:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        # Ensure SystemRoot is aliased in both title and uppercase
        if "SystemRoot" not in env and "SYSTEMROOT" in env:
            env["SystemRoot"] = env["SYSTEMROOT"]
        elif "SYSTEMROOT" not in env and "SystemRoot" in env:
            env["SYSTEMROOT"] = env["SystemRoot"]

    base_url = env.get("OMNIROUTE_BASE_URL") or DEFAULT_GATEWAY_URL
    existing_cfg: dict[str, Any] = {}
    raw_cfg = env.get("OPENCODE_CONFIG_CONTENT")
    if raw_cfg:
        try:
            parsed = _json.loads(raw_cfg)
            if isinstance(parsed, dict):
                existing_cfg = parsed
        except ValueError:
            existing_cfg = {}

    omni_prov = existing_cfg.setdefault("providers", {}).setdefault("omniroute", {})
    omni_prov.setdefault("name", "AutoOS OmniRoute gateway")
    omni_prov.setdefault("env", ["AUTOOS_OMNIROUTE_KEY"])
    omni_prov.setdefault("package", "@opencode/ai/providers/openai-compatible")
    omni_settings = omni_prov.setdefault("settings", {})
    if isinstance(omni_settings, dict):
        omni_settings.setdefault("baseURL", base_url)

    if headers:
        env["OMNIROUTE_ENVELOPE_HEADERS"] = _json.dumps(dict(headers), sort_keys=True)
        for k, v in headers.items():
            env_key = k.upper().replace("-", "_")
            env[env_key] = str(v)
        omni_headers = omni_prov.setdefault("headers", {})
        if isinstance(omni_headers, dict):
            omni_headers.update({str(k): str(v) for k, v in headers.items()})

    env["OPENCODE_CONFIG_CONTENT"] = _json.dumps(existing_cfg, sort_keys=True)

    if extra_env:
        env.update(extra_env)

    return env


def build_routing_envelope_headers(
    role: str = "implement",
    risk: str = "normal",
    min_intelligence: float | int = 44.0,
    required_context: int = 32000,
    exclude_families: Sequence[str] | str = (),
    effort: str = "medium",
    author_model: str | None = None,
) -> dict[str, str]:
    """Build structured routing envelope headers for OmniRoute ACP/Gateway.

    Headers:
      - X-OmniRoute-Task-Role: 'implement' | 'review' | 'orchestrate' | 'drive'
      - X-OmniRoute-Risk: 'low' | 'normal' | 'high' | 'critical'
      - X-OmniRoute-Min-Intelligence: str(min_intelligence)
      - X-OmniRoute-Required-Context: str(required_context)
      - X-OmniRoute-Exclude-Family: ','.join(exclude_families) (when non-empty)
      - X-OmniRoute-Effort: effort ('low' | 'medium' | 'high' | 'xhigh')
      - X-OmniRoute-Author-Model: author_model (when set)
    """
    headers: dict[str, str] = {
        "X-OmniRoute-Task-Role": str(role),
        "X-OmniRoute-Risk": str(risk),
        "X-OmniRoute-Min-Intelligence": str(min_intelligence),
        "X-OmniRoute-Required-Context": str(required_context),
        "X-OmniRoute-Effort": str(effort),
    }

    if isinstance(exclude_families, str):
        families = [f.strip().lower() for f in exclude_families.split(",") if f.strip()]
    else:
        families = [str(f).strip().lower() for f in exclude_families if str(f).strip()]

    if families:
        headers["X-OmniRoute-Exclude-Family"] = ",".join(families)

    if author_model:
        headers["X-OmniRoute-Author-Model"] = str(author_model)

    return headers


def build_opencode_command(
    prompt: str,
    model_or_combo: str,
    serve_port: int | None = None,
    headers: Mapping[str, str] | None = None,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Build opencode command for execution via OmniRoute ACP/Gateway."""
    cmd = ["opencode", "run", "--model", model_or_combo]
    if serve_port is not None:
        cmd.extend(["--port", str(serve_port)])
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(prompt)
    return cmd


def detect_client_for_model(model_id: str) -> str:
    """Identify which native CLI client maps to a model prefix when offline."""
    mid = (model_id or "").lower()
    if mid.startswith("agy/"):
        return "agy"
    if mid.startswith("qoder/"):
        return "qoder"
    if mid.startswith("cxa/"):
        return "codex"
    if mid.startswith("claude/"):
        return "claude"
    if mid.startswith("gemini/") or mid.startswith("google/"):
        return "gemini"
    if mid.startswith("qwen/"):
        return "qwen"
    return "opencode"


def build_standalone_fallback_command(
    client_name: str,
    prompt: str,
    model: str | None = None,
) -> list[str]:
    """Build direct CLI command as offline emergency fallback when gateway is offline."""
    cname = client_name.lower()
    if cname == "agy":
        cmd = ["agy", "--dangerously-skip-permissions"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-p", prompt])
        return cmd
    if cname == "qoder":
        cmd = ["qodercli", "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        return cmd
    if cname == "codex":
        return ["codex", "exec", prompt]
    if cname == "claude":
        cmd = ["claude", "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        return cmd
    # Default fallback
    cmd = ["opencode", "run"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def build_command(
    prompt: str,
    model_or_combo: str = "l2-worker",
    role: str = "implement",
    risk: str = "normal",
    gateway: bool = True,
    gateway_online: bool = True,
    client: str | None = None,
    headers: Mapping[str, str] | None = None,
    serve_port: int | None = None,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Build executable command for an agent task.

    OmniRoute ACP/Gateway is ALWAYS first (gateway=True).
    Standalone direct CLI spawning is used ONLY when gateway_online=False.
    """
    # 1. Gateway route (first priority)
    if gateway and gateway_online:
        return build_opencode_command(
            prompt=prompt,
            model_or_combo=model_or_combo,
            serve_port=serve_port,
            headers=headers,
            extra_flags=extra_flags,
        )

    # 2. Offline emergency fallback
    resolved_client = client or detect_client_for_model(model_or_combo)
    return build_standalone_fallback_command(
        client_name=resolved_client,
        prompt=prompt,
        model=model_or_combo,
    )
