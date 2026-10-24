"""OmniRoute ACP-First Client Adapter with Tailnet Coexistence.

All model providers route first through the local model gateway; its base URL is
deployment config (``OMNIROUTE_BASE_URL``, ``PLANGRAPH_GATEWAY_URL`` or the
spawner/keeper config, see ``effective_gateway_base_url``), never a source
literal. Standalone direct CLI spawning (agy,
qodercli, codex, claude) is reserved strictly as an offline emergency fallback
when gateway_online=False.

Preserves Windows SystemRoot/COMSPEC/PATHEXT/TEMP environment variables (D-2090 BUG-B).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: Environment variable naming the model gateway base URL (see ``gateway_url``).
GATEWAY_URL_ENV_VAR = "PLANGRAPH_GATEWAY_URL"

#: Environment variable naming the provider-level gateway base URL override.
OMNIROUTE_BASE_URL_ENV_VAR = "OMNIROUTE_BASE_URL"


def gateway_url(env: Mapping[str, str] | None = None,
                config: Mapping[str, Any] | None = None) -> str | None:
    """Base URL of the model gateway, or ``None`` when no source names one.

    Sources, in order: ``PLANGRAPH_GATEWAY_URL`` in *env* (the process
    environment by default), then ``gateway_url`` in the spawner/keeper *config*
    mapping.  The endpoint is deployment data, so there is no built-in default:
    a caller that has none supplies its own value (tests do).
    """
    source = os.environ if env is None else env
    for candidate in ((source or {}).get(GATEWAY_URL_ENV_VAR),
                      (config or {}).get("gateway_url")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def effective_gateway_base_url(env: Mapping[str, str] | None = None,
                               config: Mapping[str, Any] | None = None) -> str | None:
    """Base URL actually handed to gateway clients, or ``None`` when unset.

    Precedence is the one ``build_child_env`` applies when it writes ``baseURL``
    into the child provider config: ``OMNIROUTE_BASE_URL`` in *env* (the process
    environment by default) wins, else ``gateway_url`` (``PLANGRAPH_GATEWAY_URL``,
    then the *config* mapping's ``gateway_url`` key).  ``build_child_env`` and
    the ``plangraph_agent_health`` report both call this helper, so the reported
    URL cannot diverge from the effective one.
    """
    source = os.environ if env is None else env
    override = ((source or {}).get(OMNIROUTE_BASE_URL_ENV_VAR) or "").strip()
    return override or gateway_url(source, config)


#: No child-side ``settings`` node exists to honour: no ``OPENCODE_CONFIG_CONTENT``,
#: unparsable content, or a provider block ``build_child_env`` builds from scratch.
_ABSENT = object()


def _child_settings(env: Mapping[str, str]) -> Any:
    """The ``providers.omniroute.settings`` node the child's config already holds.

    Whatever JSON value sits under ``settings`` is returned as-is: a ``dict`` is
    what ``build_child_env`` merges into with ``setdefault``, any other value
    blocks the ``baseURL`` write outright.  ``_ABSENT`` means there is nothing
    to honour yet, so the deployment-config sources decide instead.
    """
    raw = (env or {}).get("OPENCODE_CONFIG_CONTENT")
    if not isinstance(raw, str) or not raw.strip():
        return _ABSENT
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _ABSENT
    node: Any = parsed
    for key in ("providers", "omniroute", "settings"):
        if not isinstance(node, dict) or key not in node:
            return _ABSENT
        node = node[key]
    return node


def effective_child_base_url(env: Mapping[str, str] | None = None,
                             config: Mapping[str, Any] | None = None) -> str | None:
    """Base URL the spawned child ends up using, or ``None`` when it uses none.

    The precedence is exactly the one ``build_child_env`` applies when it writes
    ``baseURL`` into the child provider config:

    1. an ``OPENCODE_CONFIG_CONTENT`` that already carries
       ``providers.omniroute.settings.baseURL`` keeps it -- ``setdefault`` never
       displaces a configured value, whatever the environment says;
    2. a config whose ``settings`` is not an object gets no ``baseURL`` written
       at all;
    3. only a config without that key falls back to
       ``effective_gateway_base_url`` (``OMNIROUTE_BASE_URL`` over
       ``PLANGRAPH_GATEWAY_URL`` / ``config['gateway_url']``; this module ships
       no default endpoint).

    ``build_child_env`` and the ``plangraph_agent_health`` report both read this
    one helper, so the reported URL cannot diverge from the effective one.
    """
    source = os.environ if env is None else env
    settings = _child_settings(source or {})
    if settings is _ABSENT:
        return effective_gateway_base_url(source, config)
    if not isinstance(settings, dict):
        return None                      # setdefault never runs: no baseURL
    if "baseURL" in settings:
        value = settings["baseURL"]
        return value if isinstance(value, str) else None
    return effective_gateway_base_url(source, config)


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
    config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build subprocess environment preserving critical OS and Tailnet variables.

    The gateway base URL written into the child's provider config comes from
    ``effective_child_base_url``: a config content that already carries
    ``settings.baseURL`` keeps it (``setdefault`` never displaces it), else the
    deployment sources decide (``OMNIROUTE_BASE_URL`` wins, else
    ``gateway_url``: env ``PLANGRAPH_GATEWAY_URL``, else the *config* mapping's
    ``gateway_url`` key).  When no source names one, no ``baseURL`` key is
    written at all -- this module ships no default endpoint.

    On Windows (os.name == 'nt'), ALWAYS preserves:
      - SystemRoot / SYSTEMROOT
      - COMSPEC
      - PATHEXT
      - TEMP (and TMP)
    preventing the Windows opencode serve port-in-use crash (D-2090 BUG-B).
    """
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

    # Deployment endpoint: a base URL the child's config already holds wins
    # (setdefault keeps it), else the provider-specific override or the
    # configured gateway URL (never a source default -- unset means "no baseURL
    # written"); ``effective_child_base_url`` is the one helper the health
    # report reads too.
    base_url = effective_child_base_url(env, config) or ""
    existing_cfg: dict[str, Any] = {}
    raw_cfg = env.get("OPENCODE_CONFIG_CONTENT")
    if raw_cfg:
        try:
            parsed = json.loads(raw_cfg)
            if isinstance(parsed, dict):
                existing_cfg = parsed
        except ValueError:
            existing_cfg = {}

    omni_prov = existing_cfg.setdefault("providers", {}).setdefault("omniroute", {})
    omni_prov.setdefault("name", "AutoOS OmniRoute gateway")
    omni_prov.setdefault("env", ["AUTOOS_OMNIROUTE_KEY"])
    omni_prov.setdefault("package", "@opencode/ai/providers/openai-compatible")
    omni_settings = omni_prov.setdefault("settings", {})
    if isinstance(omni_settings, dict) and base_url:
        omni_settings.setdefault("baseURL", base_url)

    if headers:
        env["OMNIROUTE_ENVELOPE_HEADERS"] = json.dumps(dict(headers), sort_keys=True)
        for k, v in headers.items():
            if k.lower() == "x-omniroute-run-nonce":
                continue  # Only carry the nonce as a request-header value.
            env_key = k.upper().replace("-", "_")
            env[env_key] = str(v)
        omni_headers = omni_prov.setdefault("headers", {})
        if isinstance(omni_headers, dict):
            omni_headers.update({str(k): str(v) for k, v in headers.items()})

    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(existing_cfg, sort_keys=True)

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
    nonce: str | None = None,
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

    if nonce:
        headers["X-OmniRoute-Run-Nonce"] = nonce

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
    if cname in ("qoder", "qodercli"):
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

    An explicit registered harness is authoritative. Calls without a harness
    retain the legacy gateway-first transport policy.
    """
    from plangraph.spawner.mimo_policy import check_mimo_path
    check_mimo_path(model_or_combo)
    if client is not None:
        if client not in (*CLIENTS, "qodercli"):
            raise ValueError(f"unknown harness: {client}")
        if client != "opencode":
            return build_standalone_fallback_command(client, prompt, model_or_combo)
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
