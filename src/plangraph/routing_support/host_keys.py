"""Host credentials a spawned client needs, from the sources AutoOS used (D-2758 (6)).

The plangraph spawner hands a child the same keys the AutoOS spawner did, from
the same host files, so no new auth design exists:

* ``AUTOOS_OMNIROUTE_KEY`` (the OmniRoute client key; the ``mimo`` and
  ``omniroute`` providers of the AutoOS ``opencode.jsonc`` read it): env wins,
  else AutoOS ``tools/autoos_gateway_key.resolve_client_key`` on
  ``$AUTOOS_ROOT/configuration/api-keys.yml`` (field ``omniroute_server`` /
  ``omniroute_<host>``).
* ``OPENCODE_API_KEY`` (the Zen key, only for ``opencode/<id>-free`` models,
  D-1214): env wins, else the ``zen`` field of the same file via AutoOS
  ``tools/keys_file.read_keys``.

A value is only ever placed in the child's environment: never returned to a
caller that prints, logged, or written to a record.  A missing source means
"no key" (the child then fails with its own auth error, as before).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

__all__ = ["GATEWAY_KEY_ENV", "ZEN_KEY_ENV", "add_host_keys", "keys_file"]

GATEWAY_KEY_ENV = "AUTOOS_OMNIROUTE_KEY"
ZEN_KEY_ENV = "OPENCODE_API_KEY"


def _autoos_root(env):
    return Path(env.get("AUTOOS_ROOT") or os.environ.get("AUTOOS_ROOT") or Path.home() / "code/AutoOS")


def keys_file(env):
    """``$AUTOOS_ROOT/configuration/api-keys.yml`` (the path only)."""
    return _autoos_root(env) / "configuration" / "api-keys.yml"


def _load(env, name):
    path = _autoos_root(env) / "tools" / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"_autoos_{name}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # an unloadable helper is "no source", like a missing file
        return None
    return module


def _gateway_key(env):
    helper = _load(env, "autoos_gateway_key")
    path = keys_file(env)
    if helper is None or not path.is_file():
        return None
    try:
        return helper.resolve_client_key(dict(env), path) or None
    except (KeyError, OSError, ValueError):
        return None


def _zen_key(env):
    helper = _load(env, "keys_file")
    path = keys_file(env)
    if helper is None or not path.is_file():
        return None
    try:
        return helper.read_keys(path).get("zen") or None
    except (OSError, ValueError):
        return None


def _is_opencode_free(model):
    text = str(model or "").strip().partition("#")[0]
    return text.startswith("opencode/") and text.endswith("-free")


def add_host_keys(env, model, *, needs_gateway):
    """Fill the child *env* in place with the host keys this run needs.

    ``needs_gateway``: the child's provider config names ``AUTOOS_OMNIROUTE_KEY``.
    An env value already present always wins.
    """
    if needs_gateway and not (env.get(GATEWAY_KEY_ENV) or "").strip():
        key = _gateway_key(env)
        if key:
            env[GATEWAY_KEY_ENV] = key
    if _is_opencode_free(model) and not (env.get(ZEN_KEY_ENV) or "").strip():
        key = _zen_key(env)
        if key:
            env[ZEN_KEY_ENV] = key
    return env
