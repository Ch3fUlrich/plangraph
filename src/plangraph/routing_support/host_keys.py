"""Hand child runs the host keys the AutoOS spawner gave us (D-2759).

The provider overlay only copies an env name through when the parent already
holds its value (``opencode_overlay.overlay_env``), and the plangraph spawner's
own environment never carries the file-managed keys -- so an opencode child
(mimo runs, ``opencode/*-free`` models) fails with "Authentication required".
These helpers resolve the keys from the AutoOS keys file at spawn time.

Contract: every failure -- a missing file, a helper that raises or exits on
import or at call time, a non-dict keys mapping, a non-str or blank key --
means "no key" (None); KeyboardInterrupt still propagates. Nothing here logs,
prints, or raises with a key value, so a lookup failure can never crash the
supervisor or leave a key unredacted in the startup tail.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

GATEWAY_KEY_ENV = "AUTOOS_OMNIROUTE_KEY"
ZEN_KEY_ENV = "OPENCODE_API_KEY"

# "opencode/<id>-free", after stripping any "#variant" suffix: the only shape
# that resolves to the free tier needing the Zen key.
_ZEN_FREE_MODEL = re.compile(r"opencode/.+-free")


def _keys_path(env):
    """<root>/configuration/api-keys.yml under the AutoOS root of `env`."""
    root = env.get("AUTOOS_ROOT") or os.environ.get("AUTOOS_ROOT") or str(Path.home() / "code/AutoOS")
    return Path(root) / "configuration" / "api-keys.yml"


def _load_helper(root, name):
    """Import <root>/tools/<name>.py by file location (never sys.path).

    None when the file is missing, unimportable, or raises or exits at import
    time (KeyboardInterrupt still propagates).
    """
    path = Path(root) / "tools" / (name + ".py")
    try:
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (Exception, SystemExit):  # a helper that raises or exits on import is "no key"
        return None


def _gateway_key(env):
    """AutoOS gateway client key from the keys file; None when unresolvable."""
    try:
        keys_path = _keys_path(env)
        if not keys_path.is_file():
            return None
        helper = _load_helper(keys_path.parent.parent, "autoos_gateway_key")
        resolve = getattr(helper, "resolve_client_key", None)
        if not callable(resolve):
            return None
        key = resolve(dict(env), keys_path)
        return key if isinstance(key, str) and key.strip() else None
    except (Exception, SystemExit):  # ANY helper failure is "no key", never a crash
        return None


def _zen_key(env):
    """Zen (opencode/*-free) key from the keys file; None when unresolvable."""
    try:
        keys_path = _keys_path(env)
        if not keys_path.is_file():
            return None
        helper = _load_helper(keys_path.parent.parent, "keys_file")
        read_keys = getattr(helper, "read_keys", None)
        if not callable(read_keys):
            return None
        keys = read_keys(keys_path)
        key = keys.get("zen") if isinstance(keys, dict) else None
        return key if isinstance(key, str) and key.strip() else None
    except (Exception, SystemExit):  # ANY helper failure is "no key", never a crash
        return None


def _absent(env, name):
    """True when env holds no usable value (empty or whitespace is none)."""
    return not str(env.get(name) or "").strip()


def _is_zen_free_model(model):
    if not isinstance(model, str):
        return False
    return _ZEN_FREE_MODEL.fullmatch(model.split("#", 1)[0].strip()) is not None


def add_host_keys(env, model, *, needs_gateway):
    """Fill `env` in place with host keys the child run needs (D-2759).

    The gateway key goes in when `needs_gateway` and env has none; the Zen key
    only for an ``opencode/<id>-free`` model and only when env has none. An
    existing env value always wins. Never raises (H5): a broken env or helper
    degrades to "no key", never a supervisor crash or a key in an exception.
    """
    try:
        if needs_gateway and _absent(env, GATEWAY_KEY_ENV):
            key = _gateway_key(env)
            if key:
                env[GATEWAY_KEY_ENV] = key
        if _is_zen_free_model(model) and _absent(env, ZEN_KEY_ENV):
            key = _zen_key(env)
            if key:
                env[ZEN_KEY_ENV] = key
    except (Exception, SystemExit):
        pass  # H5: never raise, never carry a key value outward


def redaction_env(base):
    """Copy of `base` plus both resolvable keys where absent -- redaction only."""
    env = {}
    try:
        env = dict(base)
        for name, resolve in ((GATEWAY_KEY_ENV, _gateway_key), (ZEN_KEY_ENV, _zen_key)):
            if _absent(env, name):
                key = resolve(env)
                if key:
                    env[name] = key
    except (Exception, SystemExit):
        pass  # H5: degraded redaction beats a crash; never carries a key outward
    return env
