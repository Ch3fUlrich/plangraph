"""Hand child runs the host keys the spawner or AutoOS gave us (D-2759).

The provider overlay only copies an env name through when the parent already
holds its value (``opencode_overlay.overlay_env``), and the plangraph spawner's
own environment never carries the file-managed keys -- so an opencode child
(mimo runs, ``opencode/*-free`` models) fails with "Authentication required".
These helpers resolve the gateway client key from
``~/.config/plangraph/omniroute-client.key`` first (falling back to the AutoOS
keys file only when absent) and the Zen key from the AutoOS keys file.

Contract: every failure -- a missing file, an insecure or symlinked key file, a
helper that raises or exits on import or at call time, a non-Mapping keys
result, a non-str or blank key -- means "no key" (None); KeyboardInterrupt still
propagates. Nothing here logs, prints, or raises with a key value, so a lookup
failure can never crash the supervisor or leave a key unredacted in the startup
tail.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

GATEWAY_KEY_ENV = "AUTOOS_OMNIROUTE_KEY"
ZEN_KEY_ENV = "OPENCODE_API_KEY"
_MAX_KEY_BYTES = 8192

# "opencode/<id>-free", after stripping any "#variant" suffix: the only shape
# that resolves to the free tier needing the Zen key.
_ZEN_FREE_MODEL = re.compile(r"opencode/.+-free")


def _client_key_path(env):
    """Per-host plangraph gateway client key path under HOME, or None."""
    source = os.environ if env is None else env
    home = source.get("HOME") if "HOME" in source else os.environ.get("HOME")
    if not home and os.name == "nt":
        home = source.get("USERPROFILE") if "USERPROFILE" in source else os.environ.get("USERPROFILE")
    if not isinstance(home, str) or not home.strip():
        return None
    return Path(home) / ".config" / "plangraph" / "omniroute-client.key"


def _warn_refused(path, reason, mode=None):
    """Emit one warning naming path and mode without ever logging a key value."""
    logger = logging.getLogger(__name__)
    if mode is None:
        logger.warning("refusing gateway key path=%s reason=%s mode=unknown", path, reason)
    else:
        logger.warning("refusing gateway key path=%s reason=%s mode=%04o", path, reason, stat.S_IMODE(mode))


def _check_owner_and_mode(path, st, *, dir_check=False):
    """Verify current-user ownership and 0600/0700-or-stricter POSIX permissions."""
    if os.name == "nt":
        return True
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        _warn_refused(path, "owner", st.st_mode)
        return False
    if stat.S_IMODE(st.st_mode) & 0o077:
        _warn_refused(path, "dir_mode" if dir_check else "mode", st.st_mode)
        return False
    return True


def _read_client_key_file(path):
    """Read and validate the per-host key file; return ``(found, key_or_none)``."""
    try:
        file_st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError as exc:
        _warn_refused(path, type(exc).__name__)
        return True, None
    if stat.S_ISLNK(file_st.st_mode):
        _warn_refused(path, "symlink", file_st.st_mode)
        return True, None
    if not stat.S_ISREG(file_st.st_mode):
        _warn_refused(path, "not_regular", file_st.st_mode)
        return True, None
    if not _check_owner_and_mode(path, file_st, dir_check=False):
        return True, None

    parent = path.parent
    try:
        dir_st = os.lstat(parent)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError as exc:
        _warn_refused(parent, type(exc).__name__)
        return True, None
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        _warn_refused(parent, "not_directory", dir_st.st_mode)
        return True, None
    if not _check_owner_and_mode(parent, dir_st, dir_check=True):
        return True, None

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        _warn_refused(path, type(exc).__name__, file_st.st_mode)
        return True, None
    try:
        stream = os.fdopen(fd, "rb")
    except OSError as exc:
        os.close(fd)
        _warn_refused(path, type(exc).__name__, file_st.st_mode)
        return True, None
    try:
        with stream:
            opened_st = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened_st.st_mode):
                _warn_refused(path, "not_regular", opened_st.st_mode)
                return True, None
            if not _check_owner_and_mode(path, opened_st, dir_check=False):
                return True, None
            raw = stream.read(_MAX_KEY_BYTES + 1)
    except OSError as exc:
        _warn_refused(path, type(exc).__name__, file_st.st_mode)
        return True, None

    if len(raw) > _MAX_KEY_BYTES:
        _warn_refused(path, "too_large", opened_st.st_mode)
        return True, None
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeError:
        _warn_refused(path, "invalid_utf8", opened_st.st_mode)
        return True, None
    if not key:
        return True, None
    if "\n" in key or "\r" in key:
        _warn_refused(path, "multiline", opened_st.st_mode)
        return True, None
    return True, key


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
    """Gateway client key from ~/.config/plangraph/omniroute-client.key, else AutoOS."""
    try:
        client_path = _client_key_path(env)
        if client_path is not None:
            found, key = _read_client_key_file(client_path)
            if found:
                return key
        source = os.environ if env is None else env
        keys_path = _keys_path(source)
        if not keys_path.is_file():
            return None
        helper = _load_helper(keys_path.parent.parent, "autoos_gateway_key")
        resolve = getattr(helper, "resolve_client_key", None)
        if not callable(resolve):
            return None
        key = resolve(dict(source), keys_path)
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
        key = keys.get("zen") if isinstance(keys, Mapping) else None
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
    """Copy of `base` plus both resolvable keys where absent -- redaction only.

    Never raises, never returns fewer names than it could (D-2776).
    """
    try:
        env = dict(base)
    except (Exception, SystemExit):
        env = {}
        try:
            names = list(base.keys())
        except (Exception, SystemExit):
            names = ()  # unenumerable base: the resolvable keys still land
        for name in names:
            try:
                env[name] = base[name]
            except (Exception, SystemExit):
                continue  # drop only the item whose read raises
    for name, resolve in ((GATEWAY_KEY_ENV, _gateway_key), (ZEN_KEY_ENV, _zen_key)):
        try:
            if _absent(env, name):
                key = resolve(env)
                if key:
                    env[name] = key
        except (Exception, SystemExit):
            pass  # one failing resolver never skips the other
    return env
